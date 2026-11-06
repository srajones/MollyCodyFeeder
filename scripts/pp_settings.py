#!/usr/bin/env python3
"""pp-settings — numbered settings menu for the Poor Puppy watchdog (standard library only).

Edits the project's `.env` in place (comments and unknown keys are preserved), then recreates the Docker
container so the new values take effect. Run on the VPS:  pp-settings
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(os.environ.get("POOR_PUPPY_DIR", Path(__file__).resolve().parent.parent))
ENV_FILE = PROJECT / ".env"
EXAMPLE = PROJECT / ".env.example"
SECRET_KEYS = {
    "MOLLY_PASS",
    "PASS",
    "TELEGRAM_BOT_TOKEN",
    "WEBHOOK_SECRET",
    "GEMINI_API_KEY",
    "DISCORD_WEBHOOK_URL",
    "HA_WEBHOOK_URL",
    "NTFY_TOPIC_URL",
    "REVIEW_TOKEN",
    "HEARTBEAT_URL",
}
DEFAULTS = {
    "KEEP_FOOTAGE": "true",
    "CAPTURE_RETENTION_DAYS": "30",
    "KEEP_FAILED_FOOTAGE_DAYS": "90",
    "VIDEO_KEEP_DAYS_OK": "0",
    "CAPTURE_MAX_MB": "2048",
    "DB_RETENTION_DAYS": "0",
    "EXPORT_ML_ON_PRUNE": "true",
    "ML_EXPORT_PATH": "data/ml_events.jsonl",
    "WEBHOOK_LISTEN_HOST": "127.0.0.1",
    "HA_NOTIFY_ON_SUCCESS": "true",
    "TTS_ALERT_ON_CLOG": "true",
    "TTS_VOICE": "en-US-AriaNeural",
    "TTS_VOLUME": "2.5",
    "TTS_REPEAT": "2",
}


# --------------------------------------------------------------------------- .env helpers
def read_env(path: Path = ENV_FILE) -> dict:
    vals = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
            if m:
                vals[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip()
    return vals


def write_env(updates: dict) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            seen.add(m.group(1))
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)


def shown(key: str, val: str) -> str:
    if not val:
        return "(not set)"
    return "••••" + val[-4:] if key in SECRET_KEYS and len(val) > 4 else val


# --------------------------------------------------------------------------- prompts
def ask(prompt: str, current: str = "", validate=None) -> str:
    while True:
        raw = input(f"  {prompt} [{current or 'blank'}]: ").strip()
        if raw == "":
            return current
        if raw == "-":
            return ""
        if validate is None or validate(raw):
            return raw
        print("  ✗ invalid value, try again (Enter = keep, '-' = clear)")


def is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def is_hhmm(v: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v))


def is_portions(v: str) -> bool:
    return v.isdigit() and 1 <= int(v) <= 20


def yes_no(prompt: str, current: str) -> str:
    cur = "true" if current.lower() in ("1", "true", "yes", "on") else "false"
    raw = input(f"  {prompt} (y/n) [{'y' if cur == 'true' else 'n'}]: ").strip().lower()
    return cur if raw == "" else ("true" if raw.startswith("y") else "false")


# --------------------------------------------------------------------------- menu sections
def section_alert_mode(env: dict) -> dict:
    print("\n  1) every_miss    — alert on feeds without the kibble 'ting' (2 in a row = urgent)")
    print("  2) no_food_hours — ONLY alert if no food has been seen for X hours (quiet mode)")
    choice = input(f"  Mode [{env.get('ALERT_MODE', 'every_miss')}]: ").strip()
    mode = {"1": "every_miss", "2": "no_food_hours"}.get(choice, env.get("ALERT_MODE", "every_miss"))
    upd = {"ALERT_MODE": mode}
    if mode == "no_food_hours":
        upd["NO_FOOD_ALERT_HOURS"] = ask("Alert after how many hours with no food seen", env.get("NO_FOOD_ALERT_HOURS", "8"), is_float)
        upd["FOOD_CHECK_INTERVAL_MIN"] = ask("Check the bowl every N minutes", env.get("FOOD_CHECK_INTERVAL_MIN", "60"), str.isdigit)
    return upd


def section_channels(env: dict) -> dict:
    print("\n  Where should alerts go? (Enter = keep, '-' = clear)")
    return {
        "NTFY_TOPIC_URL": ask("ntfy topic URL (e.g. https://ntfy.sh/my-puppy-x7k2)", env.get("NTFY_TOPIC_URL", "")),
        "DISCORD_WEBHOOK_URL": ask("Discord webhook URL", env.get("DISCORD_WEBHOOK_URL", "")),
        "TELEGRAM_BOT_TOKEN": ask("Telegram bot token", env.get("TELEGRAM_BOT_TOKEN", "")),
        "TELEGRAM_CHAT_ID": ask("Telegram chat id", env.get("TELEGRAM_CHAT_ID", "")),
        "HA_WEBHOOK_URL": ask("Home Assistant webhook URL", env.get("HA_WEBHOOK_URL", "")),
    }


def section_voice(env: dict) -> dict:
    upd = {
        "TTS_ALERT_ON_CLOG": yes_no("Speak clog alerts on the feeder's speaker?", env.get("TTS_ALERT_ON_CLOG", "true")),
        "TTS_VOICE": ask("TTS voice (edge-tts neural or espeak-ng fallback)", env.get("TTS_VOICE", "en-US-AriaNeural")),
        "TTS_VOLUME": ask("Speaker volume (1.5 quiet, 2.5 loud, 3.5 max)", env.get("TTS_VOLUME", "2.5"), is_float),
        "TTS_ALERT_MESSAGE": ask("What it says on clog", env.get("TTS_ALERT_MESSAGE", "")),
    }
    merged = {**env, **upd}
    act = input("  Run now?  s) Speak (custom TTS)   t) TTS test   l) Speak last alert/outcome   Enter) back: ").strip().lower()
    if act == "s":
        speak_custom_tts(merged)
    elif act == "t":
        tts_test(merged)
    elif act == "l":
        speak_last_alert(merged)
    return upd


def section_retry(env: dict) -> dict:
    return {
        "AUTO_RETRY_ON_CLOG": yes_no("Auto-retry 1 portion when a meal fails?", env.get("AUTO_RETRY_ON_CLOG", "true")),
        "MAX_DAILY_RETRY_PORTIONS": ask("Max extra portions per day", env.get("MAX_DAILY_RETRY_PORTIONS", "3"), str.isdigit),
        "NO_TING_ESCALATE_AFTER": ask("Feeds in a row without the ting before URGENT alert", env.get("NO_TING_ESCALATE_AFTER", "2"), str.isdigit),
    }


def section_schedule(env: dict) -> dict:
    return {
        "TZ": ask("Timezone of your feeder schedule (e.g. America/New_York)", env.get("TZ", "America/New_York")),
        "DAILY_SUMMARY_TIME": ask("Daily summary push time HH:MM ('-' = off)", env.get("DAILY_SUMMARY_TIME", "21:00"),
                                  lambda v: is_hhmm(v)),
        "MEAL_PREROLL_SECONDS": ask("Start watching this many seconds before each meal", env.get("MEAL_PREROLL_SECONDS", "40"), str.isdigit),
    }


def section_hopper(env: dict) -> dict:
    return {
        "HOPPER_CAPACITY_PORTIONS": ask("Portions a full hopper holds", env.get("HOPPER_CAPACITY_PORTIONS", "240"), str.isdigit),
        "HOPPER_LOW_PERCENT": ask("Warn when the hopper estimate drops below %", env.get("HOPPER_LOW_PERCENT", "20"), is_float),
    }


def section_storage(env: dict) -> dict:
    keep = yes_no("Keep captured video/audio/photos on disk? (n = delete right after analysis, keep metrics)",
                  env.get("KEEP_FOOTAGE", "true"))
    upd = {"KEEP_FOOTAGE": keep}
    if keep == "true":
        upd["CAPTURE_RETENTION_DAYS"] = ask("Delete normal footage older than N days (0 = keep forever)",
                                            env.get("CAPTURE_RETENTION_DAYS", "30"), is_float)
        upd["KEEP_FAILED_FOOTAGE_DAYS"] = ask("Keep clog/failed footage for N days",
                                              env.get("KEEP_FAILED_FOOTAGE_DAYS", "90"), is_float)
        upd["VIDEO_KEEP_DAYS_OK"] = ask("Delete only the MP4 of good feeds after N days (0 = off)",
                                        env.get("VIDEO_KEEP_DAYS_OK", "0"), is_float)
        upd["CAPTURE_MAX_MB"] = ask("Max MB for captures folder (0 = off)",
                                    env.get("CAPTURE_MAX_MB", "2048"), str.isdigit)
    upd["DB_RETENTION_DAYS"] = ask("Keep SQLite metric rows for N days (0 = forever, recommended for ML)",
                                   env.get("DB_RETENTION_DAYS", "0"), is_float)
    upd["EXPORT_ML_ON_PRUNE"] = yes_no("Append pruned event metrics to JSONL for ML training?",
                                       env.get("EXPORT_ML_ON_PRUNE", "true"))
    if upd["EXPORT_ML_ON_PRUNE"] == "true":
        upd["ML_EXPORT_PATH"] = ask("ML JSONL export path", env.get("ML_EXPORT_PATH", "data/ml_events.jsonl"))
    return upd


SECTIONS = [
    ("Alert mode (every missed feed / only if no food for X hours)", section_alert_mode,
     ["ALERT_MODE", "NO_FOOD_ALERT_HOURS"]),
    ("Alert channels (ntfy / Discord / Telegram / Home Assistant)", section_channels,
     ["NTFY_TOPIC_URL", "DISCORD_WEBHOOK_URL", "HA_WEBHOOK_URL"]),
    ("Voice alerts on the feeder speaker", section_voice, ["TTS_ALERT_ON_CLOG", "TTS_VOLUME"]),
    ("Clog retries & escalation", section_retry, ["AUTO_RETRY_ON_CLOG", "MAX_DAILY_RETRY_PORTIONS", "NO_TING_ESCALATE_AFTER"]),
    ("Timezone, daily summary, meal pre-roll", section_schedule, ["TZ", "DAILY_SUMMARY_TIME"]),
    ("Hopper estimate", section_hopper, ["HOPPER_CAPACITY_PORTIONS", "HOPPER_LOW_PERCENT"]),
    ("Retention & ML export (footage vs. SQLite metrics)", section_storage,
     ["KEEP_FOOTAGE", "CAPTURE_RETENTION_DAYS", "KEEP_FAILED_FOOTAGE_DAYS", "DB_RETENTION_DAYS", "EXPORT_ML_ON_PRUNE"]),
]


# --------------------------------------------------------------------------- docker actions
def compose(*args: str, capture: bool = False):
    if not shutil.which("docker"):
        print("  ✗ docker is not installed here (run install_ubuntu.sh first)")
        return subprocess.CompletedProcess(args, 1, "", "")
    cmd = ["docker", "compose", *args]
    if os.geteuid() != 0 and shutil.which("sudo") and not _in_docker_group():
        cmd = ["sudo", *cmd]
    return subprocess.run(cmd, cwd=PROJECT, text=True, capture_output=capture)


def _in_docker_group() -> bool:
    try:
        import grp

        return "docker" in [grp.getgrgid(g).gr_name for g in os.getgroups()]
    except Exception:
        return False


def apply_changes() -> None:
    print("\n  Applying (recreating the container with the new settings)...")
    r = compose("up", "-d")
    print("  ✓ Applied." if r.returncode == 0 else "  ✗ docker compose failed — see output above.")


def view_schedule() -> int:
    print("\n  Fetching live meal schedule from the feeder (DP 207)...")
    r = compose("exec", "feeder-watchdog", "python", "feeder_cli.py", "schedule", "list")
    return 0 if r.returncode == 0 else 1


def summarize_verify_output(stdout: str, stderr: str = "") -> str:
    import json

    idx = stdout.rfind("\n{")
    blob = stdout[idx + 1:] if idx != -1 else (stdout[stdout.find("{"):] if "{" in stdout else "")
    try:
        st = json.loads(blob)
    except Exception:
        err_src = (stderr or stdout or "").strip().splitlines()
        tail = "\n    ".join(err_src[-5:])[:300] if err_src else "no output"
        return f"  ✗ Verification failed to return JSON. Error tail:\n    {tail}"
    det = st.get("details") or {}
    aud = det.get("audio") or {}
    vis = det.get("visual") or {}
    ok = st.get("food_dispensed")
    icon = "✅" if ok else "🚨"
    lines = [
        f"  {icon} Verdict:   status={st.get('status')} | outcome={st.get('outcome')} | "
        f"food_dispensed={ok} | clog_detected={st.get('clog_detected')} (event #{st.get('db_event_id')})"
    ]
    if aud:
        lines.append(
            f"  🔊 Audio:     motor={aud.get('motor_detected')} ({aud.get('motor_duration_sec', 0):.1f}s), "
            f"kibble_peaks={aud.get('kibble_impact_peaks')}, "
            f"tings={aud.get('ting_count', 0)} ({aud.get('ting_rate_per_sec', 0):.1f}/s)"
        )
    if vis:
        b_pct = float(vis.get("before_fill_ratio") or 0) * 100
        a_pct = float(vis.get("after_fill_ratio") or 0) * 100
        d_pct = float(vis.get("fill_delta_ratio") or 0) * 100
        lines.append(
            f"  📷 Visual:    fill {b_pct:.1f}% -> {a_pct:.1f}% (delta {d_pct:+.1f}%), "
            f"food_seen={vis.get('food_present_cv')}"
        )
    media = det.get("annotated_image") or det.get("after_image") or det.get("video_clip")
    if media:
        lines.append(f"  🖼️  Capture:   {media}")
    return "\n".join(lines)


def feed_no_verify(portions: str = "") -> int:
    p = portions if is_portions(portions) else ask("Portions to dispense (1-20)", "1", is_portions)
    print(f"\n  Dispensing {p} portion(s) without camera verification...")
    r = compose("exec", "feeder-watchdog", "python", "feeder_cli.py", "feed", str(p))
    return 0 if r.returncode == 0 else 1


def feed_and_verify(portions: str = "") -> int:
    p = portions if is_portions(portions) else ask("Portions to dispense & verify (1-20)", "1", is_portions)
    print(f"\n  Dispensing {p} portion(s) + recording camera & audio verification (~15-30s)...")
    r = compose("exec", "-T", "feeder-watchdog", "python", "feeder_cli.py", "feed", str(p), "--verify", capture=True)
    summary = summarize_verify_output(r.stdout or "", r.stderr or "")
    print(summary)
    return 0 if r.returncode == 0 and not summary.startswith("  ✗") else 1


def _feeder_tts_configured(env: dict) -> bool:
    sess = PROJECT / "tuya_session.json"
    has_sess = sess.is_file() and sess.stat().st_size > 10
    has_creds = bool(env.get("MOLLY_USER") or env.get("USER")) and bool(env.get("MOLLY_PASS") or env.get("PASS"))
    return has_sess or has_creds


def _tts_configured(env: dict) -> bool:
    return _feeder_tts_configured(env) or bool(env.get("HA_WEBHOOK_URL"))


def _speak_phrase(phrase: str, env: dict, repeat: int = 1) -> bool:
    has_speaker = _feeder_tts_configured(env)
    has_ha = bool(env.get("HA_WEBHOOK_URL"))
    if not has_speaker and not has_ha:
        print("  ✗ TTS not configured: set MOLLY_USER/MOLLY_PASS (or tuya_session.json) or HA_WEBHOOK_URL in .env")
        return False
    if has_speaker:
        vol = env.get("TTS_VOLUME") or "2.5"
        voice = env.get("TTS_VOICE") or "en-US-AriaNeural"
        print(f"\n  🔊 Speaking on feeder speaker (voice={voice}, volume={vol}, repeat={repeat}): {phrase!r}")
        r = compose(
            "exec", "-T", "feeder-watchdog", "python", "feeder_cli.py",
            "say", "--volume", str(vol), "--voice", str(voice), "--repeat", str(repeat), "--", phrase,
        )
        if r.returncode == 0:
            print("  ✓ Spoken on feeder speaker.")
            return True
        print(f"  ✗ Feeder speaker TTS failed (exit code {r.returncode}).")
        if not has_ha:
            return False
        print("  ↪ Trying HA_WEBHOOK_URL TTS fallback...")
    if has_ha:
        # Never pass HA_WEBHOOK_URL on argv (ps leak); container reads HA_WEBHOOK_URL from its env_file (.env)
        r_ha = compose(
            "exec", "-T", "feeder-watchdog", "python", "-c",
            "import os,sys;from watchdog.config import WatchdogConfig;from watchdog.alerter import HouseholdAlerter;"
            "cfg=WatchdogConfig();cfg.ha_webhook_url=os.environ.get('HA_WEBHOOK_URL') or cfg.ha_webhook_url;"
            "sys.exit(0 if HouseholdAlerter(cfg).send_ha_tts(sys.argv[1]) else 1)",
            phrase,
        )
        if r_ha.returncode == 0:
            print("  ✓ Sent TTS event via HA_WEBHOOK_URL.")
            return True
        print(f"  ✗ HA_WEBHOOK_URL TTS fallback failed (exit code {r_ha.returncode}).")
    return False


def speak_custom_tts(env: dict = None, phrase: str = "") -> int:
    env = env or {**DEFAULTS, **read_env(EXAMPLE), **read_env()}
    text = phrase.strip()
    if not text:
        text = input("  Message to speak on feeder speaker (Enter = cancel): ").strip()
    if not text:
        print("  (cancelled)")
        return 1
    return 0 if _speak_phrase(text, env) else 1


def tts_test(env: dict = None, phrase: str = "") -> int:
    env = env or {**DEFAULTS, **read_env(EXAMPLE), **read_env()}
    text = phrase.strip() or ask("Phrase to speak", "Poor Puppy test")
    return 0 if _speak_phrase(text, env) else 1


def last_alert_summary_text(project_dir: Path = PROJECT) -> str:
    import sqlite3

    for rel in ("data/kibble_history.db", "data/feeder_watchdog.db", "kibble_history.db"):
        db_file = project_dir / rel
        if not db_file.exists():
            continue
        try:
            # Copy DB + WAL (never -shm) to a private temp dir so host reads never create root-owned sidecars in data/
            with tempfile.TemporaryDirectory(prefix="pp-db-ro-") as tmp_dir:
                tmp_db = Path(tmp_dir) / db_file.name
                shutil.copyfile(db_file, tmp_db)
                wal_sidecar = Path(str(db_file) + "-wal")
                if wal_sidecar.exists():
                    try:
                        shutil.copyfile(wal_sidecar, Path(str(tmp_db) + "-wal"))
                    except OSError:
                        pass
                con = sqlite3.connect(str(tmp_db), timeout=5.0)
                con.row_factory = sqlite3.Row
                cols = {r["name"] for r in con.execute("PRAGMA table_info(feed_events)").fetchall()}
                if not cols:
                    con.close()
                    continue
                select_cols = [
                    "timestamp",
                    "event_type",
                    "portions",
                    "success",
                    "clog_detected",
                    "outcome" if "outcome" in cols else "'' AS outcome",
                    "kibble_peaks" if "kibble_peaks" in cols else "0 AS kibble_peaks",
                    "after_fill_pct" if "after_fill_pct" in cols else "0.0 AS after_fill_pct",
                    "food_increased" if "food_increased" in cols else "0 AS food_increased",
                ]
                row = con.execute(
                    f"SELECT {', '.join(select_cols)} FROM feed_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
                con.close()
        except (sqlite3.Error, OSError) as exc:
            print(f"  ✗ Could not read feed history DB ({db_file}): {exc}")
            return ""
        if row:
            ts = str(row["timestamp"] or "")[:16].replace("T", " ")
            event_type = str(row["event_type"] or "")
            outcome = str(row["outcome"] or "")
            portions = int(row["portions"] or 0)
            success = bool(row["success"])
            clog = bool(row["clog_detected"])
            peaks = int(row["kibble_peaks"] or 0)
            fill = float(row["after_fill_pct"] or 0.0)
            food_increased = bool(row["food_increased"])

            # 1. On-demand bowl check ONLY when event_type or outcome is bowl_check (never infer from portions == 0)
            if event_type == "bowl_check" or outcome == "bowl_check":
                if outcome == "camera_unavailable":
                    return (
                        f"Poor Puppy notice: last bowl check at {ts} "
                        "could not run because the feeder camera was unreachable."
                    )
                if success and not clog:
                    return f"Poor Puppy status: last bowl check at {ts} saw food in the bowl ({fill:.0f} percent fill)."
                return f"Poor Puppy notice: last bowl check at {ts} found the bowl empty ({fill:.0f} percent fill)."

            # 2. Camera unreachable on a feed verification run
            if outcome == "camera_unavailable":
                for_p = f" for {portions} portions" if portions > 0 else ""
                return (
                    f"Poor Puppy notice: last feed at {ts}{for_p} "
                    "could not be verified because the feeder camera was unreachable."
                )
            if outcome == "recording_cut_short":
                for_p = f" for {portions} portions" if portions > 0 else ""
                return (
                    f"Poor Puppy notice: last feed at {ts}{for_p} "
                    "could not be verified because the camera stream dropped. It was not a clog."
                )

            # 3. Intentionally unverified cloud/manual dispense (no camera verification run)
            if event_type in ("manual_feed_unverified", "manual_feed"):
                if not success:
                    for_p = f" for {portions} portions" if portions > 0 else ""
                    return f"Poor Puppy alert: last unverified feed command at {ts}{for_p} failed to send."
                disp_p = f"dispensed {portions} portions" if portions > 0 else "dispensed food"
                return f"Poor Puppy status: last feed at {ts} {disp_p} (not camera-verified)."

            # 4. Failed / clogged feed verification (including verifier outcome='unverified' with clog_detected)
            if clog or not success:
                for_p = f" for {portions} portions" if portions > 0 else ""
                reason = (
                    "no kibble confirmed in bowl (clog or missed feed)"
                    if outcome in ("", "unverified")
                    else outcome.replace("_", " ")
                )
                return (
                    f"Poor Puppy alert: last feed at {ts}{for_p} "
                    f"reported {reason}. Please check the feeder bowl."
                )

            # 5. Successful feed verification
            disp_p = f"dispensed {portions} portions" if portions > 0 else "dispensed food"
            if peaks > 0:
                return (
                    f"Poor Puppy status: last feed at {ts} {disp_p}. "
                    f"Verified with {peaks} audio peaks and {fill:.0f} percent bowl fill."
                )
            if fill > 0.0:
                return (
                    f"Poor Puppy status: last feed at {ts} {disp_p}. "
                    f"Verified by camera (0 audio peaks, {fill:.0f} percent bowl fill)."
                )
            if food_increased:
                return (
                    f"Poor Puppy status: last feed at {ts} {disp_p}. "
                    "Verified by camera (new food detected in bowl, 0 audio peaks)."
                )
            if outcome == "unverified":
                return f"Poor Puppy status: last feed at {ts} {disp_p} (not camera-verified)."
            return (
                f"Poor Puppy status: last feed at {ts} {disp_p} "
                "(marked successful, but 0 audio peaks and 0 percent bowl fill were recorded)."
            )
    return "No feed events recorded yet. Poor Puppy watchdog is online."


def speak_last_alert(env: dict = None) -> int:
    env = env or {**DEFAULTS, **read_env(EXAMPLE), **read_env()}
    phrase = last_alert_summary_text(PROJECT)
    if not phrase:
        return 1
    return 0 if _speak_phrase(phrase, env) else 1


def run_updater(*args: str) -> int:
    """Runs this install's update-pp (safe: meal-window guard, build first, smoke test, DB backup,
    connection self-test, automatic rollback)."""
    updater = PROJECT / "update-pp"
    if not updater.exists():
        print(f"  ✗ {updater} not found")
        return 1
    r = subprocess.run(["bash", str(updater), *args], cwd=PROJECT, env={**os.environ, "POOR_PUPPY_DIR": str(PROJECT)})
    return r.returncode


def tools_menu(env: dict = None) -> None:
    env = env or {**DEFAULTS, **read_env(EXAMPLE), **read_env()}
    print(
        "\n  a) Status                   b) View feeder schedule\n"
        "  c) Feed (no verify)         d) Feed + verify (camera & audio)\n"
        "  e) Speak (custom TTS)       f) TTS test (\"Poor Puppy test\")\n"
        "  g) Speak last alert         h) Last 50 log lines\n"
        "  i) Mark hopper refilled     j) Send a test alert\n"
        "  k) Export ML bundle         l) Restart container\n"
        "  m) Update to latest version (safe: build, test, auto-rollback)\n"
        "  n) Update status            o) Connection self-test (no feed)"
    )
    c = input("  Choice: ").strip().lower()
    if c == "a":
        compose("ps")
    elif c in ("b", "schedule"):
        view_schedule()
    elif c in ("c", "feed"):
        feed_no_verify()
    elif c in ("d", "feed-verify", "verify"):
        feed_and_verify()
    elif c in ("e", "say", "speak"):
        speak_custom_tts(env)
    elif c in ("f", "tts", "tts-test"):
        tts_test(env)
    elif c in ("g", "speak-last"):
        speak_last_alert(env)
    elif c == "h":
        compose("logs", "--tail", "50", "feeder-watchdog")
    elif c == "i":
        compose("exec", "feeder-watchdog", "python", "feeder_cli.py", "refill")
    elif c == "j":
        compose("exec", "feeder-watchdog", "python", "-c",
                "from watchdog.config import WatchdogConfig;from watchdog.alerter import HouseholdAlerter;"
                "print('sent' if HouseholdAlerter(WatchdogConfig()).send_alert('Poor Puppy test alert',"
                "'If you can read this, alerts work.',event='test',urgent=False,cooldown_minutes=0) else 'suppressed')")
    elif c in ("k", "export"):
        print("  Default export packs SQLite DB + JSONL + captured media (jpg/mp4/wav). Answer 'n' for --no-media.")
        with_media = yes_no("Include captured media (jpg/mp4/wav)?", "true") == "true"
        cmd = [str(PROJECT / "scripts" / "export_ml_bundle.sh")]
        if not with_media:
            cmd.append("--no-media")
        else:
            days = ask("Only media newer than N days (0 = all)", "0", is_float)
            if float(days or 0) > 0:
                cmd += ["--days", days]
        subprocess.run(cmd, cwd=PROJECT)
    elif c == "l":
        compose("restart")
    elif c in ("m", "update"):
        run_updater("--latest")
    elif c in ("n", "update-status"):
        run_updater("--status")
    elif c in ("o", "selftest"):
        run_updater("--selftest")


def main() -> int:
    if len(sys.argv) > 1:
        sub = sys.argv[1]
        arg = sys.argv[2] if len(sys.argv) > 2 else ""
        if sub in ("update", "--update"):
            return run_updater("--latest", *sys.argv[2:])
        if sub in ("update-status", "--update-status"):
            return run_updater("--status")
        if sub in ("selftest", "--selftest"):
            return run_updater("--selftest", *sys.argv[2:])
        if sub in ("schedule", "--schedule"):
            return view_schedule()
        if sub in ("feed", "--feed"):
            return feed_no_verify(arg)
        if sub in ("feed-verify", "--feed-verify"):
            return feed_and_verify(arg)
        if sub in ("say", "speak", "--say", "--speak"):
            return speak_custom_tts(phrase=" ".join(sys.argv[2:]))
        if sub in ("tts", "tts-test", "--tts"):
            return tts_test(phrase=" ".join(sys.argv[2:]))
        if sub in ("speak-last", "--speak-last"):
            return speak_last_alert()
    if not ENV_FILE.exists():
        if EXAMPLE.exists():
            shutil.copy(EXAMPLE, ENV_FILE)
            os.chmod(ENV_FILE, 0o600)
        else:
            write_env(DEFAULTS)
    pending: dict = {}
    while True:
        env = {**DEFAULTS, **read_env(EXAMPLE), **read_env(), **pending}  # defaults + .env.example + .env
        print("\n🐶  Poor Puppy settings" + ("   (unsaved changes)" if pending else ""))
        print("  " + "-" * 60)
        for i, (title, _, keys) in enumerate(SECTIONS, 1):
            summary = ", ".join(f"{k}={shown(k, env.get(k, ''))}" for k in keys[:2])
            print(f"  {i}) {title}\n       {summary}")
        print("  8) Tools (status, schedule, feed, feed+verify, speak / TTS test / speak last, export ML, logs, restart, update)")
        print("  9) Save & apply")
        print("  0) Exit" + (" without saving" if pending else ""))
        choice = input("\n  Select: ").strip()
        if choice in [str(i) for i in range(1, len(SECTIONS) + 1)]:
            pending.update(SECTIONS[int(choice) - 1][1](env))
        elif choice == "8":
            tools_menu(env)
        elif choice == "9":
            if pending:
                write_env(pending)
                pending = {}
                apply_changes()
            else:
                print("  Nothing to save.")
        elif choice == "0":
            if pending and input("  Discard unsaved changes? (y/n): ").strip().lower() != "y":
                continue
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)
