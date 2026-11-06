import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from watchdog.paths import PROJECT_ROOT
from watchdog.db_retention import RetentionQueriesMixin

logger = logging.getLogger("poor_puppy.db")


class KibbleDatabase(RetentionQueriesMixin):
    """
    Crash-safe, corruption-proof SQLite database (`kibble_history.db`) for logging all
    manual feeds, scheduled feed verifications, acoustic/visual diagnostics, dog-in-bowl &
    IR night-vision states, and CLI/device configuration actions.

    Anti-Corruption Safeguards:
    - WAL (Write-Ahead Logging) mode (`PRAGMA journal_mode=WAL`)
    - Full fsync durability (`PRAGMA synchronous=FULL`)
    - C-level lock retry (`PRAGMA busy_timeout=15000`)
    - Thread-safe RLock + short-lived connections closed cleanly after each transaction
    - Startup `PRAGMA integrity_check` with automatic restore from online `.bak` snapshot
    - Online atomic SQLite Backup API (`conn.backup()`) after every feed event
    """

    def __init__(self, db_path: Optional[str] = None):
        default_path = PROJECT_ROOT / "kibble_history.db"
        self.db_path = Path(db_path or os.getenv("KIBBLE_DB_PATH", str(default_path)))
        self.backup_path = self.db_path.with_suffix(".db.bak")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._verify_or_recover()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _verify_or_recover(self) -> None:
        """Runs an integrity check on startup and restores from atomic backup if ever needed."""
        if not self.db_path.exists():
            return
        with self._lock:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=10.0)
                try:
                    res = conn.execute("PRAGMA quick_check;").fetchone()
                    if not res or res[0] != "ok":
                        raise sqlite3.DatabaseError(f"quick_check returned {res}")
                finally:
                    conn.close()
            except Exception as exc:
                logger.error("SQLite integrity issue detected (%s); attempting safe recovery...", exc)
                corrupt_copy = self.db_path.with_suffix(f".corrupt.{int(datetime.now().timestamp())}")
                try:
                    shutil.move(str(self.db_path), str(corrupt_copy))
                except Exception:
                    pass
                if self.backup_path.exists():
                    shutil.copy2(str(self.backup_path), str(self.db_path))
                    logger.info("Restored SQLite database from clean backup %s", self.backup_path)

    def _online_backup(self, src_conn: sqlite3.Connection) -> None:
        """Uses SQLite's native online backup API to maintain a hot, consistent backup copy
        (throttled to once per 10 minutes: a full copy on every event is wasted disk I/O)."""
        now = time.monotonic()
        if self.backup_path.exists() and now - getattr(self, "_last_backup", -1e9) < 600:
            return
        self._last_backup = now
        try:
            bak_conn = sqlite3.connect(str(self.backup_path), timeout=10.0)
            try:
                src_conn.backup(bak_conn)
            finally:
                bak_conn.close()
        except Exception as exc:
            logger.debug("Online SQLite backup skipped: %s", exc)

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS feed_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            event_type TEXT NOT NULL,
                            mode TEXT,
                            transport TEXT,
                            portions INTEGER DEFAULT 0,
                            success INTEGER NOT NULL,
                            clog_detected INTEGER NOT NULL,
                            audio_state TEXT,
                            motor_detected INTEGER DEFAULT 0,
                            kibble_peaks INTEGER DEFAULT 0,
                            motor_db REAL DEFAULT 0.0,
                            impact_db REAL DEFAULT 0.0,
                            before_fill_pct REAL DEFAULT 0.0,
                            after_fill_pct REAL DEFAULT 0.0,
                            fill_delta_pct REAL DEFAULT 0.0,
                            bowl_empty INTEGER DEFAULT 0,
                            food_increased INTEGER DEFAULT 0,
                            dog_in_bowl INTEGER DEFAULT 0,
                            infrared_mode INTEGER DEFAULT 0,
                            food_weight_g INTEGER,
                            feed_abnormal INTEGER DEFAULT 0,
                            before_image TEXT,
                            after_image TEXT,
                            annotated_image TEXT,
                            video_clip TEXT,
                            audio_clip TEXT,
                            webhook_sent INTEGER DEFAULT 0,
                            diagnosis TEXT,
                            details_json TEXT
                        );

                        CREATE INDEX IF NOT EXISTS idx_feed_events_ts ON feed_events(timestamp DESC);

                        CREATE TABLE IF NOT EXISTS action_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            action TEXT NOT NULL,
                            transport TEXT,
                            success INTEGER NOT NULL,
                            summary TEXT,
                            payload_json TEXT
                        );

                        CREATE INDEX IF NOT EXISTS idx_action_logs_ts ON action_logs(timestamp DESC);

                        CREATE TABLE IF NOT EXISTS food_observations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            source TEXT NOT NULL,
                            detail TEXT
                        );

                        CREATE TABLE IF NOT EXISTS hopper_refills (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            capacity_portions INTEGER NOT NULL,
                            note TEXT
                        );
                        """
                    )
                    # Additive migrations (old DBs keep working; new columns default to NULL)
                    have = {r[1] for r in conn.execute("PRAGMA table_info(feed_events)").fetchall()}
                    for col, decl in (
                        ("outcome", "TEXT"),
                        ("motor_duration_sec", "REAL"),
                        ("kibble_rate", "REAL"),
                        ("media_purged_at", "TEXT"),
                        ("ml_exported_at", "TEXT"),
                    ):
                        if col not in have:
                            conn.execute(f"ALTER TABLE feed_events ADD COLUMN {col} {decl}")
            finally:
                conn.close()

    def log_feed_event(
        self,
        event_type: str,
        mode: str,
        transport: str,
        portions: int,
        success: bool,
        clog_detected: bool,
        audio_state: str = "unverified",
        motor_detected: bool = False,
        kibble_peaks: int = 0,
        motor_db: float = 0.0,
        impact_db: float = 0.0,
        before_fill_pct: float = 0.0,
        after_fill_pct: float = 0.0,
        fill_delta_pct: float = 0.0,
        bowl_empty: bool = False,
        food_increased: bool = False,
        dog_in_bowl: bool = False,
        infrared_mode: bool = False,
        food_weight_g: Optional[int] = None,
        feed_abnormal: int = 0,
        before_image: Optional[str] = None,
        after_image: Optional[str] = None,
        annotated_image: Optional[str] = None,
        video_clip: Optional[str] = None,
        audio_clip: Optional[str] = None,
        webhook_sent: bool = False,
        diagnosis: str = "",
        details: Optional[Dict[str, Any]] = None,
        outcome: Optional[str] = None,
        motor_duration_sec: Optional[float] = None,
        kibble_rate: Optional[float] = None,
    ) -> int:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO feed_events (
                            timestamp, event_type, mode, transport, portions,
                            success, clog_detected, audio_state, motor_detected,
                            kibble_peaks, motor_db, impact_db, before_fill_pct,
                            after_fill_pct, fill_delta_pct, bowl_empty, food_increased,
                            dog_in_bowl, infrared_mode, food_weight_g, feed_abnormal,
                            before_image, after_image, annotated_image, video_clip,
                            audio_clip, webhook_sent, diagnosis, details_json,
                            outcome, motor_duration_sec, kibble_rate
                        ) VALUES (
                            ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?
                        )
                        """,
                        (
                            ts,
                            event_type,
                            mode,
                            transport,
                            int(portions),
                            1 if success else 0,
                            1 if clog_detected else 0,
                            audio_state,
                            1 if motor_detected else 0,
                            int(kibble_peaks),
                            round(float(motor_db), 2),
                            round(float(impact_db), 2),
                            round(float(before_fill_pct), 2),
                            round(float(after_fill_pct), 2),
                            round(float(fill_delta_pct), 2),
                            1 if bowl_empty else 0,
                            1 if food_increased else 0,
                            1 if dog_in_bowl else 0,
                            1 if infrared_mode else 0,
                            int(food_weight_g) if food_weight_g is not None else None,
                            int(feed_abnormal),
                            before_image,
                            after_image,
                            annotated_image,
                            video_clip,
                            audio_clip,
                            1 if webhook_sent else 0,
                            diagnosis,
                            json.dumps(details or {}, default=str),
                            outcome,
                            motor_duration_sec,
                            kibble_rate,
                        ),
                    )
                    row_id = int(cur.lastrowid or 0)
                self._online_backup(conn)
                return row_id
            finally:
                conn.close()

    def log_action(
        self,
        action: str,
        transport: str,
        success: bool,
        summary: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO action_logs (timestamp, action, transport, success, summary, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ts,
                            action,
                            transport,
                            1 if success else 0,
                            summary,
                            json.dumps(payload or {}),
                        ),
                    )
                    return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM feed_events ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # ------------------------------------------------------------------ owner labels (v0.6, training data)
    def _ensure_labels_table(self, conn) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS feed_labels ("
            " key TEXT PRIMARY KEY, event_id INTEGER, label_json TEXT NOT NULL, labeled_at TEXT NOT NULL)"
        )

    def get_labels(self) -> Dict[str, Any]:
        """All owner labels, keyed '<event timestamp>|<event id>' (same keys as ml/labels/feed_event_labels.json)."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    self._ensure_labels_table(conn)
                rows = conn.execute("SELECT key, label_json FROM feed_labels").fetchall()
            finally:
                conn.close()
        out: Dict[str, Any] = {}
        for key, raw in rows:
            try:
                out[key] = json.loads(raw)
            except (TypeError, ValueError):
                continue
        return out

    def save_label(self, key: str, label: Dict[str, Any], merge: bool = False,
                   labeled_at: Optional[str] = None) -> Dict[str, Any]:
        """Upserts one label (merge=True keeps existing fields, e.g. a quick 👍/👎 on top of a full label)."""
        label = dict(label)
        try:
            event_id = int(str(key).rsplit("|", 1)[1])
        except (IndexError, ValueError):
            event_id = None
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    self._ensure_labels_table(conn)
                    if merge:
                        row = conn.execute("SELECT label_json FROM feed_labels WHERE key = ?", (key,)).fetchone()
                        if row:
                            try:
                                label = {**json.loads(row[0]), **label}
                            except (TypeError, ValueError):
                                pass
                    label["labeled_at"] = labeled_at or datetime.now().isoformat(timespec="seconds")
                    conn.execute(
                        "INSERT INTO feed_labels (key, event_id, label_json, labeled_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET label_json = excluded.label_json, labeled_at = excluded.labeled_at",
                        (key, event_id, json.dumps(label), label["labeled_at"]),
                    )
            finally:
                conn.close()
        return label

    def seed_labels(self, labels: Dict[str, Any]) -> int:
        """Imports labels that aren't in the DB yet (never overwrites labels made on the server)."""
        have = self.get_labels()
        added = 0
        for key, lab in (labels or {}).items():
            if key not in have and isinstance(lab, dict):
                self.save_label(key, lab, labeled_at=lab.get("labeled_at"))
                added += 1
        return added

    def get_recent_actions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM action_logs ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_last_feed(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM feed_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def get_events_on(self, date_prefix: str) -> List[Dict[str, Any]]:
        """All feed events whose local timestamp starts with `YYYY-MM-DD`."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM feed_events WHERE timestamp LIKE ? ORDER BY id", (date_prefix + "%",)
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def record_food_evidence(self, source: str, detail: str = "") -> None:
        """Something showed the dog has food: 'ting' (verified feed) or 'camera' (food seen in the bowl)."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO food_observations (timestamp, source, detail) VALUES (?, ?, ?)",
                        (datetime.now().isoformat(timespec="seconds"), source, detail),
                    )
            finally:
                conn.close()

    def last_food_evidence(self) -> Optional[Dict[str, Any]]:
        """Most recent evidence of food (observation or verified feed), or None."""
        with self._lock:
            conn = self._connect()
            try:
                obs = conn.execute(
                    "SELECT timestamp, source FROM food_observations ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                feed = conn.execute(
                    "SELECT timestamp FROM feed_events WHERE success = 1 AND outcome = 'dispensed' "
                    "ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        cands = []
        if obs:
            cands.append({"timestamp": obs["timestamp"], "source": obs["source"]})
        if feed:
            cands.append({"timestamp": feed["timestamp"], "source": "ting"})
        return max(cands, key=lambda c: c["timestamp"]) if cands else None

    def consecutive_missed_feeds(self) -> int:
        """How many of the most recent feeds (that expected food) in a row had no kibble sound.
        Unverified runs (camera unreachable, recording cut short by a dropped stream) neither count nor reset."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT outcome FROM feed_events WHERE event_type IN "
                    "('scheduled_verify', 'manual_dispense_verify', 'auto_retry_verify') "
                    "AND outcome IS NOT NULL "
                    "AND outcome NOT IN ('camera_unavailable', 'unverified', 'recording_cut_short') "
                    "ORDER BY id DESC LIMIT 20"
                ).fetchall()
            finally:
                conn.close()
        n = 0
        for r in rows:
            if r["outcome"] not in ("motor_no_kibble", "no_motor"):
                break
            n += 1
        return n

    def portions_today(self, event_type: str) -> int:
        """Sum of portions logged today (local date) for one event type — used by the overfeed guard."""
        prefix = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COALESCE(SUM(portions), 0) FROM feed_events WHERE event_type = ? AND timestamp LIKE ?",
                    (event_type, prefix + "%"),
                ).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()

    def record_refill(self, capacity_portions: int, note: str = "") -> None:
        """Marks the hopper as refilled (portion counter restarts from here)."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO hopper_refills (timestamp, capacity_portions, note) VALUES (?, ?, ?)",
                        (datetime.now().isoformat(timespec="seconds"), int(capacity_portions), note),
                    )
            finally:
                conn.close()

    def hopper_status(self, default_capacity: int) -> Dict[str, Any]:
        """Estimated kibble left = capacity - portions the motor actually ran for since the last refill."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT timestamp, capacity_portions FROM hopper_refills ORDER BY id DESC LIMIT 1"
                ).fetchone()
                since = row["timestamp"] if row else None
                capacity = int(row["capacity_portions"]) if row else int(default_capacity)
                # Kibble left the hopper if the feed succeeded, or the motor ran and it wasn't a
                # "motor spun but nothing came out" clog
                q = (
                    "SELECT COALESCE(SUM(portions), 0) FROM feed_events WHERE (success = 1 OR "
                    "(motor_detected = 1 AND COALESCE(outcome, '') != 'motor_no_kibble'))"
                )
                args: tuple = ()
                if since:
                    q += " AND timestamp >= ?"
                    args = (since,)
                used = int(conn.execute(q, args).fetchone()[0] or 0)
            finally:
                conn.close()
        left = max(0, capacity - used)
        return {
            "known": since is not None,
            "refilled_at": since,
            "capacity_portions": capacity,
            "portions_used": used,
            "portions_left": left,
            "percent_left": round(100.0 * left / capacity, 1) if capacity > 0 else None,
        }

    def learned_baselines(self, min_samples: int = 3) -> Dict[str, Optional[float]]:
        """This feeder's own normal motor seconds per portion and kibble-impact rate (medians of
        recent verified feeds) so warnings adapt to the actual hardware / room acoustics."""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT portions, motor_duration_sec, kibble_rate FROM feed_events "
                    "WHERE success = 1 AND outcome = 'dispensed' AND motor_duration_sec > 0 "
                    "ORDER BY id DESC LIMIT 40"
                ).fetchall()
            finally:
                conn.close()
        rates = [r["kibble_rate"] for r in rows if r["kibble_rate"]]
        spp = [r["motor_duration_sec"] / r["portions"] for r in rows if r["portions"]]

        def med(v):
            return float(sorted(v)[len(v) // 2]) if len(v) >= min_samples else None

        return {"kibble_rate": med(rates), "motor_sec_per_portion": med(spp)}

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0]
                succ = conn.execute(
                    "SELECT COUNT(*) FROM feed_events WHERE success = 1"
                ).fetchone()[0]
                failed = conn.execute(
                    "SELECT COUNT(*) FROM feed_events WHERE success = 0"
                ).fetchone()[0]
                clogs = conn.execute(
                    "SELECT COUNT(*) FROM feed_events WHERE clog_detected = 1"
                ).fetchone()[0]
                portions = (
                    conn.execute(
                        "SELECT COALESCE(SUM(portions), 0) FROM feed_events WHERE success = 1"
                    ).fetchone()[0]
                    or 0
                )
                dog_seen = conn.execute(
                    "SELECT COUNT(*) FROM feed_events WHERE dog_in_bowl = 1"
                ).fetchone()[0]
                ir_events = conn.execute(
                    "SELECT COUNT(*) FROM feed_events WHERE infrared_mode = 1"
                ).fetchone()[0]
                row = conn.execute(
                    "SELECT * FROM feed_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
                last = dict(row) if row else None
                return {
                    "total_events": int(total),
                    "successful_events": int(succ),
                    "failed_events": int(failed),
                    "clogs_detected": int(clogs),
                    "total_portions_dispensed": int(portions),
                    "dog_at_bowl_events": int(dog_seen),
                    "infrared_night_events": int(ir_events),
                    "success_rate_pct": round((succ / total) * 100.0, 1) if total > 0 else 100.0,
                    "last_event": last,
                }
            finally:
                conn.close()
