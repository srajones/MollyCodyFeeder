"""Local HTTP API (Home Assistant webhooks, status, snapshot) — the seed of a future web UI.

Routes are declared in `GET_ROUTES` / `POST_ROUTES` (path prefix -> handler method name) so adding an
endpoint is one line plus one small method. All routes except `/health` require WEBHOOK_SECRET when set.

`/health` stays liveness-cheap (no cloud calls) but also reports `busy` and the next/last meal timing so
`update-pp` never restarts the container in the middle of a meal verification. `/selftest` is the post-update
comms check: it reuses the daemon's existing cloud session (never a password login) and never dispenses.
"""
import hmac
import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import schedule

from watchdog.schedule_decoder import decode_molly_schedule, parse_days_spec

logger = logging.getLogger("poor_puppy.http_api")

MAX_BODY_BYTES = 64 * 1024
FRESH_SNAPSHOT_MIN_INTERVAL_SEC = 10  # each fresh snapshot opens a camera session (~5 s)
# /selftest holds the verification lock; refuse it this close to a meal so it can't delay the recording
SELFTEST_MEAL_MARGIN_SEC = 120


def meal_timing(preroll_seconds: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Seconds until the next / since the last scheduled meal, from the in-memory `meal_checks` jobs only
    (no cloud calls). Each meal check job fires `preroll_seconds` before the meal time and repeats daily."""
    out: Dict[str, Any] = {"meal_count": 0, "next_meal_in_sec": None, "last_meal_ago_sec": None}
    try:
        jobs = [
            j for j in schedule.get_jobs("meal_checks")
            if "meal_portions" in (getattr(j.job_func, "keywords", None) or {}) and j.next_run is not None
        ]
    except Exception:  # job list mutated concurrently by a schedule re-sync: report unknown, never fail /health
        return out
    if not jobs:
        return out
    now = now or datetime.now()
    nexts, lasts = [], []
    for job in jobs:
        delta = (job.next_run - now).total_seconds() + max(0, int(preroll_seconds))
        if delta > 0:
            nexts.append(delta)
            lasts.append(86400 - delta)
        else:  # the check is running right now (schedule reschedules a job only after it returns)
            nexts.append(delta + 86400)
            lasts.append(-delta)
    out.update(meal_count=len(jobs), next_meal_in_sec=int(min(nexts)), last_meal_ago_sec=int(max(0, min(lasts))))
    return out


def _check(name: str, status: str, detail: str) -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def run_selftest(watchdog, camera: bool = False) -> Dict[str, Any]:
    """Post-update connection self-test (caller holds `watchdog._busy`).

    Never performs a password login (existing session only, `allow_relogin=False`) and never dispenses food.
    Statuses: pass / warn / fail / skip. A FAIL makes `update-pp` roll back; feeder offline is only a WARN
    (a feeder losing Wi-Fi is not caused by an update)."""
    started = time.monotonic()
    w, cfg, molly = watchdog, watchdog.cfg, watchdog.molly
    checks: List[Dict[str, str]] = []

    def _clean(exc: Exception) -> str:
        msg = f"{type(exc).__name__}: {exc}"
        for secret in (getattr(molly, "sid", ""), getattr(molly, "ecode", "")):
            if secret:
                msg = msg.replace(str(secret), "***")
        return msg[:240]

    # 1. Cloud API with the EXISTING session (or LAN in lan-only mode)
    cloud_ok = False
    mode = str(getattr(molly, "mode", cfg.feeder_mode) or "").lower()
    if mode == "lan":
        try:
            lan_ok = molly.get_dps_lan() is not None
        except Exception as exc:
            lan_ok = False
            logger.debug("Self-test LAN read failed: %s", _clean(exc))
        cloud_ok = lan_ok
        checks.append(_check("feeder_lan", "pass" if lan_ok else "fail",
                             "read DPs over LAN" if lan_ok else "could not read DPs over LAN (FEEDER_MODE=lan)"))
    elif not (getattr(molly, "sid", "") and getattr(molly, "uid", "") and getattr(molly, "ecode", "")):
        checks.append(_check("cloud_api", "fail",
                             "no saved cloud session in the daemon (self-test never logs in; check the logs)"))
    else:
        try:
            cloud_ok = bool(molly.refresh_cloud_devices(allow_relogin=False))
            checks.append(_check(
                "cloud_api", "pass" if cloud_ok else "fail",
                f"session valid, device '{getattr(molly, 'device_name', '') or getattr(molly, 'device_id', '')}' found"
                if cloud_ok else "cloud API rejected the existing session or the feeder is missing from the account",
            ))
        except Exception as exc:
            checks.append(_check("cloud_api", "fail", f"cloud API unreachable: {_clean(exc)}"))

    # 2. Feeder online (WARN only)
    online = bool(getattr(molly, "is_online", False)) if cloud_ok else False
    if not cloud_ok:
        checks.append(_check("feeder_online", "skip", "skipped (no fresh cloud/LAN data)"))
    else:
        checks.append(_check("feeder_online", "pass" if online else "warn",
                             "feeder reports online" if online else "feeder reports OFFLINE (power / Wi-Fi?)"))

    # 3. Schedule DP 207 readable (from the data refreshed above)
    if not cloud_ok:
        checks.append(_check("schedule_dp207", "skip", "skipped (no fresh cloud/LAN data)"))
    else:
        raw = str((getattr(molly, "cached_dps", None) or {}).get(str(cfg.dp_schedule or "207"), "") or "")
        try:
            enabled = sorted({m.time_str for m in decode_molly_schedule(raw) if m.enabled}) if raw else []
            if not raw:
                checks.append(_check("schedule_dp207", "warn", "feeder returned no DP 207 schedule value"))
            elif enabled:
                checks.append(_check("schedule_dp207", "pass", f"{len(enabled)} enabled meal(s): {', '.join(enabled)}"))
            else:
                checks.append(_check("schedule_dp207", "warn", "DP 207 readable but no meals are enabled"))
        except Exception as exc:
            checks.append(_check("schedule_dp207", "fail", f"could not decode DP 207: {_clean(exc)}"))

    # 4. SQLite quick_check
    try:
        con = sqlite3.connect(str(w.db.db_path), timeout=10.0)
        try:
            res = con.execute("PRAGMA quick_check;").fetchone()
        finally:
            con.close()
        ok = bool(res) and res[0] == "ok"
        checks.append(_check("database", "pass" if ok else "fail", "quick_check ok" if ok else f"quick_check: {res}"))
    except Exception as exc:
        checks.append(_check("database", "fail", f"could not open DB: {_clean(exc)}"))

    # 5. Alert channel configured (nothing is sent)
    channels = [name for name, on in (
        ("ntfy", cfg.ntfy_topic_url),
        ("discord", cfg.discord_webhook_url),
        ("telegram", cfg.telegram_bot_token and cfg.telegram_chat_id),
        ("home_assistant", cfg.ha_webhook_url),
    ) if on]
    checks.append(_check("alert_channels", "pass" if channels else "warn",
                         ", ".join(channels) if channels else "no alert channel configured (pp-settings -> 2)"))

    # 6. Optional deep check: one camera snapshot (no dispense)
    if camera:
        if not cloud_ok:
            checks.append(_check("camera", "skip", "skipped (cloud check failed)"))
        elif not online:
            checks.append(_check("camera", "skip", "skipped (feeder offline)"))
        else:
            snap = w.work_dir / "selftest_snapshot.jpg"
            ok = False
            try:
                for _ in range(2):  # one retry: camera relays are occasionally slow to answer
                    if molly.capture_snapshot(str(snap)) and snap.exists() and snap.stat().st_size > 0:
                        ok = True
                        break
            except Exception as exc:
                logger.debug("Self-test snapshot failed: %s", _clean(exc))
            finally:
                try:
                    snap.unlink(missing_ok=True)
                except OSError:
                    pass
            checks.append(_check("camera", "pass" if ok else "fail",
                                 "captured a camera snapshot" if ok else "could not capture a camera snapshot"))

    statuses = {c["status"] for c in checks}
    overall = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    return {"overall": overall, "checks": checks, "duration_sec": round(time.monotonic() - started, 1)}


class WatchdogApiHandler(BaseHTTPRequestHandler):
    """One request. `watchdog` is bound per server via `make_handler()`."""

    watchdog: Any = None

    GET_ROUTES = {
        "/status": "get_status",
        "/schedule": "get_schedule",
        "/history": "get_history",
        "/hopper": "get_hopper",
        "/snapshot.jpg": "get_snapshot",
        "/selftest": "get_selftest",
        "/events": "get_events",
        "/alerts": "get_alerts",
        "/label": "get_label_page",
        "/api/events": "get_api_events",
        "/export": "get_export",
        "/ha/summary.json": "get_ha_summary",
        "/ha": "get_ha_page",
        "/system": "get_system_page",
        "/nav.js": "get_nav_js",
        "/api/settings": "get_api_settings",
        "/api/agent": "get_api_agent",
    }
    POST_ROUTES = {
        "/api/label": "post_api_label",
        "/api/agent": "post_api_agent",
        "/webhook/feedback": "post_feedback",
        "/webhook/check": "post_check",
        "/webhook/feed": "post_feed",
        "/webhook/schedule": "post_schedule",
        "/webhook/mode": "post_mode",
        "/webhook/refill": "post_refill",
        "/webhook/say": "post_say",
    }
    # These all use the feeder's single camera/speaker session (or dispense): never during a meal recording
    BUSY_SENSITIVE = ("/webhook/check", "/webhook/feed", "/webhook/say")

    # ------------------------------------------------------------------ plumbing
    def _authorized(self) -> bool:
        secret = self.watchdog.cfg.webhook_secret
        if not secret:
            return True
        token = self.headers.get("X-Webhook-Token", "")
        if not token:
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.lower().startswith("bearer ") else auth
        # Constant-time exact match
        return hmac.compare_digest(token.strip().encode("utf-8"), secret.encode("utf-8"))

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("Body too large")
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
        try:
            return json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}

    @staticmethod
    def _route(routes: Dict[str, str], path: str):
        for prefix, name in routes.items():
            if path.startswith(prefix):
                return prefix, name
        return None, None

    def do_GET(self):
        if self.path.startswith("/health"):
            # Liveness only: no cloud calls (Docker probes this every 60 s). `busy` / meal timing are
            # in-memory reads used by update-pp's meal-window guard.
            w = self.watchdog
            payload = {"ok": True, "status": w.latest_state.get("status"), "busy": bool(w._busy.locked())}
            payload.update(meal_timing(getattr(w.cfg, "meal_preroll_seconds", 0)))
            self._send_json(200, payload)
            return
        if not self._authorized():
            self._send_json(401, {"error": "Unauthorized"})
            return
        if self.path.split("?", 1)[0] in ("/", "/ui"):
            self.get_ui()
            return
        _, name = self._route(self.GET_ROUTES, self.path)
        if not name:
            self._send_json(404, {"error": "Not found"})
            return
        getattr(self, name)()

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "Unauthorized"})
            return
        try:
            body = self._read_json()
        except ValueError:
            self._send_json(413, {"error": "Body too large"})
            return
        prefix, name = self._route(self.POST_ROUTES, self.path)
        if not name:
            self._send_json(404, {"error": "Unknown webhook endpoint"})
            return
        if prefix in self.BUSY_SENSITIVE and self.watchdog._busy.locked():
            self._send_json(409, {"error": "A verification is already running; try again shortly"})
            return
        getattr(self, name)(body)

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def _in_background(self, **verify_kwargs) -> None:
        threading.Thread(target=self.watchdog.verify_feed_event, kwargs=verify_kwargs, daemon=True).start()

    # ------------------------------------------------------------------ GET endpoints
    def get_status(self):
        payload = dict(self.watchdog.latest_state)
        payload["feeder"] = self.watchdog.molly.get_status()
        self._send_json(200, payload)

    def get_schedule(self):
        raw, meals = self.watchdog.molly.get_schedule()
        self._send_json(200, {"schedule_raw": raw, "slots": [m.to_dict() for m in meals]})

    def get_history(self):
        db = self.watchdog.db
        self._send_json(200, {
            "stats": db.get_stats(),
            "recent_events": db.get_recent_events(25),
            "recent_actions": db.get_recent_actions(25),
        })

    def get_hopper(self):
        self._send_json(200, self.watchdog.db.hopper_status(self.watchdog.cfg.hopper_capacity_portions))

    def get_ui(self):
        """Phone-friendly control page (snapshot, speak, feed, bowl check, status). Behind nginx basic auth,
        which also injects the API token, so the page itself never holds a secret."""
        page = Path(__file__).resolve().parent.parent / "web" / "templates" / "control.html"
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def get_snapshot(self):
        w = self.watchdog
        want_fresh = "fresh=1" in self.path or not w.latest_snapshot_path.exists()
        # Rate-limit live captures (each opens a camera session) and never steal the camera mid-meal
        if (want_fresh and not w._busy.locked()
                and time.monotonic() - w._last_fresh_snapshot >= FRESH_SNAPSHOT_MIN_INTERVAL_SEC):
            w._last_fresh_snapshot = time.monotonic()
            w.molly.capture_snapshot(str(w.latest_snapshot_path))
        if w.latest_snapshot_path.exists():
            data = w.latest_snapshot_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Snapshot-Taken", str(int(w.latest_snapshot_path.stat().st_mtime)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_json(404, {"error": "No snapshot captured yet"})

    def get_selftest(self):
        """Connection self-test for update-pp: serialized with verifications via the daemon's `_busy` lock."""
        w = self.watchdog
        timing = meal_timing(getattr(w.cfg, "meal_preroll_seconds", 0))
        nxt = timing.get("next_meal_in_sec")
        if nxt is not None and nxt <= int(getattr(w.cfg, "meal_preroll_seconds", 0)) + SELFTEST_MEAL_MARGIN_SEC:
            self._send_json(409, {"error": "A meal check starts shortly; try the self-test after the meal",
                                  "next_meal_in_sec": nxt})
            return
        if not w._busy.acquire(blocking=False):
            self._send_json(409, {"error": "A verification is already running; try again shortly"})
            return
        try:
            report = run_selftest(w, camera="camera=1" in self.path)
        finally:
            w._busy.release()
        self._send_json(503 if report["overall"] == "fail" else 200, report)

    # ------------------------------------------------------------------ feed timeline + labelling (control page)
    EVENT_IMAGE_KINDS = ("after", "annotated", "before")
    # kind -> (DB column, allowed suffixes, content type)
    EVENT_MEDIA = {
        "after": ("after_image", (".jpg", ".jpeg"), "image/jpeg"),
        "annotated": ("annotated_image", (".jpg", ".jpeg"), "image/jpeg"),
        "before": ("before_image", (".jpg", ".jpeg"), "image/jpeg"),
        "video": ("video_clip", (".mp4",), "video/mp4"),
        "audio": ("audio_clip", (".wav",), "audio/wav"),
    }

    def _event_media_path(self, ev: Optional[Dict[str, Any]], kind: str) -> Optional[Path]:
        """Stored media of one feed, only if it still exists INSIDE the captures folder (retention may purge it)."""
        if not ev or kind not in self.EVENT_MEDIA:
            return None
        col, suffixes, _ = self.EVENT_MEDIA[kind]
        raw = ev.get(col)
        if not raw:
            return None
        root = Path(self.watchdog.work_dir).resolve()
        p = Path(str(raw))
        candidates = ([p] if p.is_absolute() else []) + [root / p.name]  # same file name in the captures folder
        for c in candidates:
            try:
                c = c.resolve()
            except OSError:
                continue
            if c.suffix.lower() in suffixes and root in c.parents and c.is_file():
                return c
        return None

    def _event_image_path(self, ev: Dict[str, Any], kind: str) -> Optional[Path]:  # backward-compatible name
        return self._event_media_path(ev, kind)

    def _send_file(self, path: Path, ctype: str) -> None:
        """Serves a file with HTTP Range support (iPhone Safari won't play MP4 without it)."""
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range") or ""
        partial = False
        if rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                elif b:
                    start = max(0, size - int(b))
                end = min(end, size - 1)
                partial = start <= end
            except ValueError:
                partial = False
            if not partial:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser aborted (normal while seeking video)

    def _find_event(self, ev_id: int) -> Optional[Dict[str, Any]]:
        return next((e for e in self.watchdog.db.get_recent_events(1000) if int(e.get("id", -1)) == ev_id), None)

    def _labels(self) -> Dict[str, Any]:
        """Owner labels from the DB; the labels committed in the repo are imported once (never overwritten)."""
        w = self.watchdog
        if not getattr(w, "_labels_seeded", False):
            w._labels_seeded = True
            seed = Path(__file__).resolve().parents[2] / "ml" / "labels" / "feed_event_labels.json"
            try:
                if seed.is_file():
                    n = w.db.seed_labels(json.loads(seed.read_text(encoding="utf-8")))
                    if isinstance(n, int) and n:
                        logger.info("Imported %d owner labels from %s", n, seed)
            except Exception as exc:
                logger.warning("Could not import repo labels: %s", exc)
        return w.db.get_labels()

    @staticmethod
    def _query_int(path: str, name: str, default: int, lo: int, hi: int) -> int:
        q = path.split("?", 1)[1] if "?" in path else ""
        for kv in q.split("&"):
            if kv.startswith(name + "="):
                try:
                    return max(lo, min(hi, int(kv[len(name) + 1:])))
                except ValueError:
                    pass
        return default

    def get_events(self):
        """GET /events?limit=N -> compact feed list; GET /events/<id>/<kind>.<ext> -> that feed's photo/video/audio."""
        parts = [x for x in self.path.split("?", 1)[0].split("/") if x]
        if len(parts) == 3:
            try:
                ev_id = int(parts[1])
            except ValueError:
                self._send_json(400, {"error": "bad event id"})
                return
            kind = parts[2].rsplit(".", 1)[0]
            if kind not in self.EVENT_MEDIA:
                self._send_json(404, {"error": "unknown media kind"})
                return
            path = self._event_media_path(self._find_event(ev_id), kind)
            if path is None:
                self._send_json(404, {"error": "not kept (deleted by footage retention or never captured)"})
                return
            self._send_file(path, self.EVENT_MEDIA[kind][2])
            return
        limit = self._query_int(self.path, "limit", 60, 1, 200)
        labels = self._labels()
        out = []
        for e in self.watchdog.db.get_recent_events(limit):
            try:
                details = json.loads(e.get("details_json") or "{}")
            except (TypeError, ValueError):
                details = {}
            warn = details.get("warnings") if isinstance(details.get("warnings"), dict) else {}
            audio = details.get("audio") if isinstance(details.get("audio"), dict) else {}
            lab = labels.get(f"{e.get('timestamp')}|{e.get('id')}") or {}
            out.append({
                "id": e.get("id"),
                "timestamp": e.get("timestamp"),
                "event_type": e.get("event_type"),
                "portions": e.get("portions"),
                "success": bool(e.get("success")),
                "clog_detected": bool(e.get("clog_detected")),
                "outcome": e.get("outcome"),
                "diagnosis": e.get("diagnosis"),
                "motor_sec": e.get("motor_duration_sec"),
                "tings_per_sec": audio.get("ting_rate_per_sec"),
                "kibble_coverage": audio.get("kibble_coverage"),
                "flags": [k for k in ("weak_flow", "short_run", "low_flow", "recording_cut_short") if warn.get(k)],
                "photos": [k for k in self.EVENT_IMAGE_KINDS if self._event_media_path(e, k)],
                "has_video": self._event_media_path(e, "video") is not None,
                "has_audio": self._event_media_path(e, "audio") is not None,
                "feedback": lab.get("quick_verdict"),
                "labeled": bool(lab),
            })
        self._send_json(200, {"events": out})

    def get_alerts(self):
        """Alerts that actually went out (recorded since v0.6)."""
        limit = self._query_int(self.path, "limit", 50, 1, 200)
        rows = [a for a in self.watchdog.db.get_recent_actions(limit * 3) if a.get("action") == "alert"][:limit]
        out = []
        for a in rows:
            try:
                payload = json.loads(a.get("payload_json") or "{}")
            except (TypeError, ValueError):
                payload = {}
            out.append({"timestamp": a.get("timestamp"), "title": a.get("summary"), "event": payload.get("event"),
                        "urgent": bool(payload.get("urgent")), "message": payload.get("message")})
        self._send_json(200, {"alerts": out})

    def get_label_page(self):
        page = Path(__file__).resolve().parents[2] / "ml" / "review_bundle.html"
        if not page.is_file():
            self._send_json(404, {"error": "labelling page not installed"})
            return
        body = page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def get_api_events(self):
        """Same JSON shape as ml/review_bundle.py, so the labelling page works on the live server too."""
        limit = self._query_int(self.path, "limit", 200, 1, 1000)
        events = []
        for e in reversed(self.watchdog.db.get_recent_events(limit)):  # oldest first, like the bundle tool
            row = dict(e)
            try:
                row["details"] = json.loads(row.pop("details_json", None) or "{}")
            except (TypeError, ValueError):
                row["details"] = {}
            row["media"] = {}
            row["media_names"] = {}
            for kind, (col, _, _) in self.EVENT_MEDIA.items():
                p = self._event_media_path(e, kind)
                ext = (p.suffix if p else "")
                row["media"][col] = f"/events/{e.get('id')}/{kind}{ext}" if p else None
                row["media_names"][col] = p.name if p else None
            events.append(row)
        self._send_json(200, {"bundle": "live server", "db": Path(str(self.watchdog.db.db_path)).name,
                              "events": events, "labels": self._labels()})

    def get_export(self):
        """GET /export?media=none|audio|all&days=N -> streamed poor-puppy-ml-<ts>.tar.gz in the same layout as
        scripts/export_ml_bundle.sh (data/<db>.db, captures/*, manifest.json) + labels/feed_event_labels.json,
        so ml/review_bundle.py and ml/experiments/*.py work on it directly. Streams (no big temp files)."""
        import gzip
        import sqlite3 as _sq
        import tarfile
        import tempfile
        from datetime import datetime as _dt, timedelta as _td

        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
        media = params.get("media", "audio")
        if media not in ("none", "audio", "all"):
            self._send_json(400, {"error": "media must be none, audio or all"})
            return
        days = self._query_int(self.path, "days", 0, 0, 3650)
        kinds = {"none": (), "audio": ("audio",), "all": tuple(self.EVENT_MEDIA)}[media]
        w = self.watchdog
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        name = f"poor-puppy-ml-{stamp}"
        cutoff = (_dt.now() - _td(days=days)).isoformat() if days else ""
        events = [e for e in w.db.get_recent_events(100000) if not cutoff or str(e.get("timestamp", "")) >= cutoff]

        tmpdir = tempfile.mkdtemp(prefix="pp-export-")
        db_copy = Path(tmpdir) / Path(str(w.db.db_path)).name
        try:
            src = _sq.connect(str(w.db.db_path), timeout=30)
            dst = _sq.connect(str(db_copy))
            try:
                src.backup(dst)  # consistent online snapshot while the daemon keeps running
            finally:
                dst.close()
                src.close()
        except Exception as exc:
            self._send_json(500, {"error": f"could not snapshot the database: {exc}"})
            return

        labels = self._labels()
        files = []
        for e in events:
            for k in kinds:
                p = self._event_media_path(e, k)
                if p is not None:
                    files.append(p)
        manifest = {
            "exported_at": _dt.now().astimezone().isoformat(timespec="seconds"),
            "source": "control page /export",
            "options": {"media": media, "days": days or None},
            "feed_events": {"total": len(events), "success": sum(1 for e in events if e.get("success")),
                            "labeled": sum(1 for e in events if f"{e.get('timestamp')}|{e.get('id')}" in labels)},
            "files": len(files),
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{name}.tar.gz"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def add_bytes(tar, arcname, data: bytes):
            info = tarfile.TarInfo(arcname)
            info.size, info.mtime = len(data), int(time.time())
            import io
            tar.addfile(info, io.BytesIO(data))

        try:
            with gzip.GzipFile(fileobj=self.wfile, mode="wb", compresslevel=1) as gz, \
                    tarfile.open(fileobj=gz, mode="w|") as tar:
                add_bytes(tar, f"{name}/manifest.json", json.dumps(manifest, indent=2).encode())
                add_bytes(tar, f"{name}/labels/feed_event_labels.json", json.dumps(labels, indent=2, sort_keys=True).encode())
                tar.add(str(db_copy), arcname=f"{name}/data/{db_copy.name}")
                for p in files:
                    tar.add(str(p), arcname=f"{name}/captures/{p.name}")
        except (BrokenPipeError, ConnectionResetError):
            logger.info("Export download cancelled by the browser")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.close_connection = True

    # ------------------------------------------------------------------ Home Assistant (RESTful sensors)
    @staticmethod
    def _meal_result(e: Dict[str, Any]) -> Tuple[str, str]:
        """(short code, plain text) for one feed, matching the control page wording."""
        try:
            warn = (json.loads(e.get("details_json") or "{}").get("warnings") or {})
        except (TypeError, ValueError, AttributeError):
            warn = {}
        if e.get("outcome") == "bowl_check" or e.get("event_type") == "bowl_check":
            return ("food_in_bowl", "Food in bowl") if e.get("success") else ("bowl_empty", "Bowl looks empty")
        if e.get("success"):
            if isinstance(warn, dict) and (warn.get("weak_flow") or warn.get("short_run") or warn.get("low_flow")):
                return "weak_flow", "Fed, weak kibble flow"
            return "fed", "Fed"
        return {
            "recording_cut_short": ("unverified", "Couldn't verify (camera dropped)"),
            "camera_unavailable": ("unverified", "Couldn't verify (camera offline)"),
            "motor_no_kibble": ("problem", "No kibble: clog or empty hopper?"),
            "no_motor": ("problem", "Feeder didn't run"),
        }.get(e.get("outcome"), ("problem", "Not confirmed"))

    def ha_summary(self) -> Dict[str, Any]:
        """Flat, Home-Assistant-friendly summary (one REST call -> many sensors). Local data only, no cloud calls."""
        from datetime import datetime as _dt, timedelta as _td

        w = self.watchdog
        now = _dt.now()
        iso = lambda d: d.astimezone().isoformat(timespec="seconds") if d else None  # noqa: E731 (tz-aware for HA)
        parse = lambda s: _dt.fromisoformat(str(s)) if s else None  # noqa: E731
        events = w.db.get_recent_events(300)
        meals = [e for e in events if e.get("event_type") != "bowl_check"]
        last = meals[0] if meals else None
        today = [e for e in meals if str(e.get("timestamp", "")).startswith(now.date().isoformat())]
        week = [e for e in meals if (parse(e.get("timestamp")) or now) >= now - _td(days=7)]
        timing = meal_timing(getattr(w.cfg, "meal_preroll_seconds", 0))
        nxt = timing.get("next_meal_in_sec")
        hop = w.db.hopper_status(w.cfg.hopper_capacity_portions)
        fed_week = [e for e in week if e.get("success")]
        per_day = (sum(int(e.get("portions") or 0) for e in fed_week) / 7.0) if fed_week else 0.0

        def tings(e):
            try:
                return (json.loads(e.get("details_json") or "{}").get("audio") or {}).get("ting_rate_per_sec")
            except (TypeError, ValueError, AttributeError):
                return None

        flows = [t for t in (tings(e) for e in week) if isinstance(t, (int, float))]
        alerts = [a for a in w.db.get_recent_actions(60) if a.get("action") == "alert"]
        code, text = self._meal_result(last) if last else (None, None)
        today_codes = [self._meal_result(e)[0] for e in today]
        busy = bool(w._busy.locked())
        last_dt = parse(last.get("timestamp")) if last else None
        return {
            "status": "busy" if busy else ("problem" if code == "problem" else "ok"),
            "busy": busy,
            "last_meal_time": iso(last_dt),
            "last_meal_result": code,
            "last_meal_text": text,
            "last_meal_ok": bool(last and last.get("success")),
            "last_meal_portions": int(last.get("portions") or 0) if last else None,
            "minutes_since_last_meal": round((now - last_dt).total_seconds() / 60) if last_dt else None,
            "next_meal_time": iso(now + _td(seconds=nxt)) if nxt is not None else None,
            "minutes_to_next_meal": round(nxt / 60) if nxt is not None else None,
            "today_meals": len(today),
            "today_fed": sum(1 for c in today_codes if c in ("fed", "weak_flow")),
            "today_problems": sum(1 for c in today_codes if c in ("problem", "unverified")),
            "today_portions": sum(int(e.get("portions") or 0) for e in today if e.get("success")),
            "today_list": [{"time": iso(parse(e.get("timestamp"))), "result": self._meal_result(e)[0],
                            "text": self._meal_result(e)[1], "portions": e.get("portions")} for e in reversed(today)],
            "week_meals": len(week),
            "week_confirmed_pct": round(100.0 * len(fed_week) / len(week)) if week else None,
            "kibble_flow": tings(last) if last else None,
            "kibble_flow_7d_avg": round(sum(flows) / len(flows), 2) if flows else None,
            "hopper_known": bool(hop.get("known")),
            "hopper_percent": hop.get("percent_left") if hop.get("known") else None,
            "hopper_portions_left": hop.get("portions_left") if hop.get("known") else None,
            "hopper_days_left": round(hop["portions_left"] / per_day, 1) if hop.get("known") and per_day > 0 else None,
            "last_alert_title": alerts[0].get("summary") if alerts else None,
            "last_alert_time": iso(parse(alerts[0].get("timestamp"))) if alerts else None,
            "updated": iso(now),
        }

    def get_ha_summary(self):
        self._send_json(200, self.ha_summary())

    def get_ha_page(self):
        self._send_page("ha.html")

    def _send_page(self, name: str, ctype: str = "text/html; charset=utf-8") -> None:
        folder = "static" if name.endswith(".js") else "templates"
        body = (Path(__file__).resolve().parent.parent / "web" / folder / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def get_system_page(self):
        self._send_page("system.html")

    def get_nav_js(self):
        self._send_page("nav.js", "application/javascript; charset=utf-8")

    # ------------------------------------------------------------------ Settings page <-> host helper (pp_agent.py)
    AGENT_ACTIONS = ("apply_settings", "rotate_token", "set_password", "update", "selftest", "status", "restart", "refresh")

    def _agent_dir(self) -> Path:
        return Path(str(self.watchdog.db.db_path)).resolve().parent / "agent"

    def get_api_settings(self):
        snap = self._agent_dir() / "settings.json"
        if not snap.is_file():
            self._send_json(503, {"error": "The settings helper isn't installed on the VPS yet. Run: sudo ./vps_install.sh",
                                  "installed": False})
            return
        try:
            self._send_json(200, {"installed": True, **json.loads(snap.read_text())})
        except (OSError, ValueError) as exc:
            self._send_json(500, {"error": f"could not read settings: {exc}"})

    def get_api_agent(self):
        """GET /api/agent/<id> -> that request's result (pending / running / done)."""
        parts = [x for x in self.path.split("?", 1)[0].split("/") if x]
        if len(parts) != 3 or not all(c.isalnum() or c in "-_" for c in parts[2]):
            self._send_json(400, {"error": "use /api/agent/<id>"})
            return
        rid = parts[2]
        res = self._agent_dir() / "results" / f"{rid}.json"
        if res.is_file():
            try:
                self._send_json(200, json.loads(res.read_text()))
                return
            except (OSError, ValueError):
                pass
        pending = (self._agent_dir() / "requests" / f"{rid}.json").is_file()
        self._send_json(200 if pending or res.exists() else 404, {"id": rid, "status": "pending" if pending else "unknown"})

    def post_api_agent(self, body):
        """Queue one whitelisted action for the host helper. Passwords are hashed HERE; only the hash leaves."""
        import secrets as _secrets

        action, params = body.get("action"), body.get("params") or {}
        if action not in self.AGENT_ACTIONS or not isinstance(params, dict):
            self._send_json(400, {"error": "unknown action"})
            return
        agent = self._agent_dir()
        if not (agent / "settings.json").is_file() or not (agent / "requests").is_dir():
            self._send_json(503, {"error": "The settings helper isn't installed on the VPS yet. Run: sudo ./vps_install.sh"})
            return
        if action == "set_password":
            pw, user = str(params.get("password") or ""), str(params.get("username") or "").strip()
            if len(pw) < 12 or len(pw) > 200:
                self._send_json(400, {"error": "password must be 12-200 characters"})
                return
            if not user or not all(c.isalnum() or c in "._-" for c in user) or not 3 <= len(user) <= 32:
                self._send_json(400, {"error": "username must be 3-32 letters/digits/._-"})
                return
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                import crypt  # stdlib on Python 3.11 (the image's Python)
            params = {"username": user, "hash": crypt.crypt(pw, crypt.mksalt(crypt.METHOD_SHA512))}
        if len(json.dumps(params)) > 20000:
            self._send_json(413, {"error": "request too large"})
            return
        rid = f"{time.strftime('%Y%m%d%H%M%S')}-{_secrets.token_hex(4)}"
        req = agent / "requests" / f"{rid}.json"
        tmp = req.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": rid, "action": action, "params": params, "requested_at": time.time()}))
        tmp.replace(req)  # atomic: the systemd path unit only ever sees complete *.json files
        self._send_json(202, {"id": rid, "status": "pending"})

    # ------------------------------------------------------------------ POST endpoints
    def post_api_label(self, body):
        """Labelling page: save one full label (same format as ml/labels/feed_event_labels.json)."""
        key, label = body.get("key"), body.get("label")
        if not isinstance(key, str) or "|" not in key or not isinstance(label, dict) or len(json.dumps(label)) > 50000:
            self._send_json(400, {"error": "need {key: '<timestamp>|<id>', label: {...}}"})
            return
        self._labels()  # make sure repo labels are imported first
        self._send_json(200, {"ok": True, "label": self.watchdog.db.save_label(key, label)})

    def post_feedback(self, body):
        """Control page 👍/👎: 'was the system's verdict right?' — merged into that feed's label."""
        verdict = body.get("verdict")
        try:
            ev_id = int(body.get("id"))
        except (TypeError, ValueError):
            ev_id = -1
        ev = self._find_event(ev_id) if ev_id >= 0 else None
        if ev is None or verdict not in ("right", "wrong", None):
            self._send_json(400, {"error": "need {id: <feed id>, verdict: 'right'|'wrong'|null}"})
            return
        key = f"{ev.get('timestamp')}|{ev.get('id')}"
        self._labels()
        note = str(body.get("note") or "").strip()[:500]
        extra = {"quick_verdict": verdict, "event_id": ev.get("id"), "system_outcome": ev.get("outcome"),
                 "system_success": bool(ev.get("success"))}
        if note:
            extra["quick_note"] = note
        self._send_json(200, {"ok": True, "label": self.watchdog.db.save_label(key, extra, merge=True)})
    def post_check(self, body):
        self._in_background(trigger_dispense=False)
        self._send_json(202, {"status": "verification_started"})

    def post_feed(self, body):
        try:
            portions = int(body.get("portions", 1))
        except (TypeError, ValueError):
            portions = 0
        if not 1 <= portions <= 20:  # the feeder accepts 1-20 portions per command
            self._send_json(400, {"error": "portions must be 1-20"})
            return
        if bool(body.get("verify", True)):
            self._in_background(trigger_dispense=True, portions=portions)
            self._send_json(202, {"status": "dispense_and_verify_started", "portions": portions})
            return
        ok, transport = self.watchdog.molly.dispense_food(portions)
        self._send_json(200 if ok else 500,
                        {"status": "dispensed" if ok else "failed", "portions": portions, "transport": transport})

    def post_schedule(self, body):
        try:
            hour, minute = body.get("hour"), body.get("minute")
            if "time" in body and ":" in str(body["time"]):
                h_s, m_s = str(body["time"]).split(":", 1)
                hour, minute = int(h_s), int(m_s)
            ok, transport, updated = self.watchdog.molly.update_schedule_slot(
                slot=int(body["slot"]),
                hour=hour,
                minute=minute,
                portions=body.get("portions"),
                days_mask=parse_days_spec(str(body["days"])) if "days" in body else None,
                enabled=body.get("enabled"),
            )
            self.watchdog.sync_schedule_jobs()
            self._send_json(200 if ok else 500, {
                "status": "updated" if ok else "failed",
                "transport": transport,
                "schedule": [m.to_dict() for m in updated],
            })
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def post_mode(self, body):
        try:
            new_mode = self.watchdog.molly.set_mode(str(body.get("mode", "auto")))
            self.watchdog.latest_state["mode"] = new_mode
            self._send_json(200, {"status": "ok", "mode": new_mode})
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def post_refill(self, body):
        cap = int(body.get("capacity_portions") or self.watchdog.cfg.hopper_capacity_portions)
        self.watchdog.db.record_refill(cap, note=str(body.get("note") or "webhook"))
        self._send_json(200, self.watchdog.db.hopper_status(cap))

    def post_say(self, body):
        cfg = self.watchdog.cfg
        text = str(body.get("text") or body.get("message") or "").strip()
        if not text:
            self._send_json(400, {"error": "Missing 'text' field"})
            return
        res = self.watchdog.molly.speak_tts(
            text=text,
            voice=str(body.get("voice") or cfg.tts_voice),
            volume=float(body.get("volume") or cfg.tts_volume),
            repeat=int(body.get("repeat") or 1),
            prepend_chime=bool(body.get("chime", True)),
        )
        self.watchdog.db.log_action(action="webhook_say", transport="cloud_imm", success=bool(res.get("ok")),
                                    summary=f"Webhook TTS: {text}", payload=res)
        self._send_json(200 if res.get("ok") else 500, res)


def make_handler(watchdog) -> type:
    """Binds a watchdog instance to a handler class (one per server)."""
    return type("BoundWatchdogApiHandler", (WatchdogApiHandler,), {"watchdog": watchdog})


class HttpApiMixin:
    """Starts the local HTTP API in a background thread."""

    def start_webhook_server(self) -> ThreadingHTTPServer:
        host, port = self.cfg.webhook_listen_host, self.cfg.webhook_listen_port
        if host not in ("127.0.0.1", "localhost", "::1") and not self.cfg.webhook_secret:
            logger.warning(
                "HTTP API is listening on %s WITHOUT a WEBHOOK_SECRET: anyone who can reach it can "
                "dispense food and view the camera. Set WEBHOOK_SECRET or WEBHOOK_LISTEN_HOST=127.0.0.1.",
                host,
            )
        server = ThreadingHTTPServer((host, port), make_handler(self))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Started HTTP API on %s:%d (GET %s, POST %s)", host, port,
                    ", ".join(["/health", *WatchdogApiHandler.GET_ROUTES]), ", ".join(WatchdogApiHandler.POST_ROUTES))
        return server
