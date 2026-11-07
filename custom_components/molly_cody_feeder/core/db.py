"""SQLite WAL state store (`KibbleDatabase`) for the Molly & Cody Smart Feeder."""
from __future__ import annotations

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

from custom_components.molly_cody_feeder.core.paths import DATA_DIR

logger = logging.getLogger("molly_cody_feeder.db")

DEFAULT_DB_PATH = str(DATA_DIR / "kibble_history.db")

_EVENT_Migrations = (
    ("outcome", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("motor_duration_sec", "REAL NOT NULL DEFAULT 0.0"),
    ("kibble_rate", "REAL NOT NULL DEFAULT 0.0"),
    ("user_feedback", "TEXT"),
    ("feedback_note", "TEXT"),
    ("feedback_at", "TEXT"),
    ("files_purged", "INTEGER NOT NULL DEFAULT 0"),
    ("exported_at", "TEXT"),
)


class KibbleDatabase:
    """Crash-safe SQLite database with WAL mode, online backup, hopper tracking, and learned baselines."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or os.getenv("KIBBLE_DB_PATH", DEFAULT_DB_PATH)).resolve()
        self.backup_path = self.db_path.with_suffix(".db.bak")
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_healthy_and_initialized()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _check_integrity(self) -> bool:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return True
        try:
            conn = self._connect()
            try:
                res = conn.execute("PRAGMA integrity_check;").fetchone()
                return bool(res and res[0] == "ok")
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            logger.error("SQLite integrity check failed on %s: %s", self.db_path, exc)
            return False

    def _ensure_healthy_and_initialized(self) -> None:
        with self._lock:
            if not self._check_integrity():
                corrupt_copy = self.db_path.with_suffix(f".corrupt.{int(time.time())}.db")
                logger.warning("Database %s appears corrupted! Quarantining to %s", self.db_path, corrupt_copy)
                try:
                    shutil.move(str(self.db_path), str(corrupt_copy))
                except Exception as exc:
                    logger.error("Failed to quarantine corrupt DB: %s", exc)
                if self.backup_path.exists() and self.backup_path.stat().st_size > 0:
                    logger.info("Restoring database from clean backup %s", self.backup_path)
                    shutil.copy2(str(self.backup_path), str(self.db_path))

            conn = self._connect()
            try:
                with conn:
                    conn.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS feed_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            unix_time INTEGER NOT NULL,
                            event_type TEXT NOT NULL,
                            mode TEXT NOT NULL,
                            transport TEXT NOT NULL,
                            portions INTEGER NOT NULL DEFAULT 0,
                            success INTEGER NOT NULL,
                            clog_detected INTEGER NOT NULL,
                            audio_state TEXT NOT NULL,
                            motor_detected INTEGER NOT NULL,
                            kibble_peaks INTEGER NOT NULL DEFAULT 0,
                            motor_energy_db REAL NOT NULL DEFAULT 0.0,
                            impact_energy_db REAL NOT NULL DEFAULT 0.0,
                            before_fill_ratio REAL NOT NULL DEFAULT 0.0,
                            after_fill_ratio REAL NOT NULL DEFAULT 0.0,
                            fill_delta_ratio REAL NOT NULL DEFAULT 0.0,
                            frame_diff_ratio REAL NOT NULL DEFAULT 0.0,
                            is_infrared INTEGER NOT NULL DEFAULT 0,
                            dog_at_bowl INTEGER NOT NULL DEFAULT 0,
                            pre_weight_g INTEGER,
                            post_weight_g INTEGER,
                            feed_abnormal INTEGER NOT NULL DEFAULT 0,
                            diagnosis TEXT NOT NULL DEFAULT '',
                            before_image TEXT,
                            after_image TEXT,
                            annotated_image TEXT,
                            video_clip TEXT,
                            audio_clip TEXT,
                            raw_json TEXT NOT NULL DEFAULT '{}'
                        );
                        CREATE INDEX IF NOT EXISTS idx_feed_events_time ON feed_events(unix_time DESC);

                        CREATE TABLE IF NOT EXISTS action_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            unix_time INTEGER NOT NULL,
                            action TEXT NOT NULL,
                            transport TEXT NOT NULL,
                            success INTEGER NOT NULL,
                            summary TEXT NOT NULL,
                            payload_json TEXT NOT NULL DEFAULT '{}'
                        );
                        CREATE INDEX IF NOT EXISTS idx_action_logs_time ON action_logs(unix_time DESC);

                        CREATE TABLE IF NOT EXISTS hopper_refills (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            unix_time INTEGER NOT NULL,
                            capacity_portions INTEGER NOT NULL,
                            note TEXT NOT NULL DEFAULT '',
                            after_event_id INTEGER NOT NULL DEFAULT 0
                        );
                        CREATE INDEX IF NOT EXISTS idx_hopper_refills_time ON hopper_refills(unix_time DESC);

                        CREATE TABLE IF NOT EXISTS food_observations (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            unix_time INTEGER NOT NULL,
                            source TEXT NOT NULL,
                            detail TEXT NOT NULL DEFAULT ''
                        );
                        CREATE INDEX IF NOT EXISTS idx_food_obs_time ON food_observations(unix_time DESC);
                        """
                    )
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_events)").fetchall()}
                    for col, ddl in _EVENT_Migrations:
                        if col not in cols:
                            conn.execute(f"ALTER TABLE feed_events ADD COLUMN {col} {ddl}")
                    refill_cols = {r[1] for r in conn.execute("PRAGMA table_info(hopper_refills)").fetchall()}
                    if "after_event_id" not in refill_cols:
                        conn.execute("ALTER TABLE hopper_refills ADD COLUMN after_event_id INTEGER NOT NULL DEFAULT 0")
            finally:
                conn.close()
            self._perform_online_backup()

    def _perform_online_backup(self) -> None:
        try:
            src = sqlite3.connect(str(self.db_path), timeout=15.0)
            dst = sqlite3.connect(str(self.backup_path), timeout=15.0)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
                src.close()
        except Exception as exc:
            logger.debug("Online SQLite backup warning: %s", exc)

    def log_feed_event(
        self,
        event_type: str,
        mode: str,
        transport: str,
        portions: int,
        success: bool,
        clog_detected: bool,
        audio_state: str = "unknown",
        motor_detected: bool = False,
        kibble_peaks: int = 0,
        motor_energy_db: float = 0.0,
        impact_energy_db: float = 0.0,
        before_fill_ratio: float = 0.0,
        after_fill_ratio: float = 0.0,
        fill_delta_ratio: float = 0.0,
        frame_diff_ratio: float = 0.0,
        is_infrared: bool = False,
        dog_at_bowl: bool = False,
        pre_weight_g: Optional[int] = None,
        post_weight_g: Optional[int] = None,
        feed_abnormal: int = 0,
        diagnosis: str = "",
        before_image: Optional[str] = None,
        after_image: Optional[str] = None,
        annotated_image: Optional[str] = None,
        video_clip: Optional[str] = None,
        audio_clip: Optional[str] = None,
        raw_metrics: Optional[Dict[str, Any]] = None,
        outcome: str = "unknown",
        motor_duration_sec: float = 0.0,
        kibble_rate: float = 0.0,
    ) -> int:
        now_ts = int(time.time())
        iso_ts = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        raw_json = json.dumps(raw_metrics or {}, default=str)

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO feed_events (
                            timestamp, unix_time, event_type, mode, transport, portions,
                            success, clog_detected, audio_state, motor_detected, kibble_peaks,
                            motor_energy_db, impact_energy_db, before_fill_ratio, after_fill_ratio,
                            fill_delta_ratio, frame_diff_ratio, is_infrared, dog_at_bowl,
                            pre_weight_g, post_weight_g, feed_abnormal, diagnosis,
                            before_image, after_image, annotated_image, video_clip, audio_clip, raw_json,
                            outcome, motor_duration_sec, kibble_rate
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            iso_ts,
                            now_ts,
                            event_type,
                            mode,
                            transport,
                            int(portions),
                            1 if success else 0,
                            1 if clog_detected else 0,
                            audio_state,
                            1 if motor_detected else 0,
                            int(kibble_peaks),
                            float(motor_energy_db),
                            float(impact_energy_db),
                            float(before_fill_ratio),
                            float(after_fill_ratio),
                            float(fill_delta_ratio),
                            float(frame_diff_ratio),
                            1 if is_infrared else 0,
                            1 if dog_at_bowl else 0,
                            pre_weight_g,
                            post_weight_g,
                            int(feed_abnormal),
                            diagnosis,
                            before_image,
                            after_image,
                            annotated_image,
                            video_clip,
                            audio_clip,
                            raw_json,
                            outcome,
                            float(motor_duration_sec),
                            float(kibble_rate),
                        ),
                    )
                    row_id = int(cur.lastrowid or 0)
            finally:
                conn.close()
            self._perform_online_backup()
            return row_id

    def record_feed_event(self, payload: Dict[str, Any]) -> int:
        return self.log_feed_event(
            event_type=str(payload.get("event_type", "scheduled_verify")),
            mode=str(payload.get("mode", "cloud")),
            transport=str(payload.get("transport", "cloud")),
            portions=int(payload.get("portions", 1)),
            success=bool(payload.get("success", True)),
            clog_detected=bool(payload.get("clog_detected", False)),
            audio_state=str(payload.get("audio_state", "unknown")),
            motor_detected=bool(payload.get("motor_detected", False)),
            kibble_peaks=int(payload.get("kibble_peaks", 0)),
            motor_energy_db=float(payload.get("motor_energy_db", 0.0)),
            impact_energy_db=float(payload.get("impact_energy_db", 0.0)),
            before_fill_ratio=float(payload.get("before_fill_ratio", 0.0)),
            after_fill_ratio=float(payload.get("after_fill_ratio", 0.0)),
            fill_delta_ratio=float(payload.get("fill_delta_ratio", 0.0)),
            frame_diff_ratio=float(payload.get("frame_diff_ratio", 0.0)),
            is_infrared=bool(payload.get("is_infrared", False)),
            dog_at_bowl=bool(payload.get("dog_at_bowl", False)),
            pre_weight_g=payload.get("pre_weight_g"),
            post_weight_g=payload.get("post_weight_g"),
            feed_abnormal=int(payload.get("feed_abnormal", 0)),
            diagnosis=str(payload.get("diagnosis", "")),
            before_image=payload.get("before_image"),
            after_image=payload.get("after_image"),
            annotated_image=payload.get("annotated_image"),
            video_clip=payload.get("video_clip"),
            audio_clip=payload.get("audio_clip"),
            raw_metrics=payload.get("raw_metrics"),
            outcome=str(payload.get("outcome", "unknown")),
            motor_duration_sec=float(payload.get("motor_duration_sec", 0.0)),
            kibble_rate=float(payload.get("kibble_rate", 0.0)),
        )

    def log_action(
        self,
        action: str,
        transport: str,
        success: bool,
        summary: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        now_ts = int(time.time())
        iso_ts = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO action_logs (timestamp, unix_time, action, transport, success, summary, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            iso_ts,
                            now_ts,
                            action,
                            transport,
                            1 if success else 0,
                            summary,
                            json.dumps(payload or {}, default=str),
                        ),
                    )
                    row_id = int(cur.lastrowid or 0)
            finally:
                conn.close()
            self._perform_online_backup()
            return row_id

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM feed_events ORDER BY unix_time DESC, id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.get_recent_events(limit=limit)

    def get_recent_actions(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM action_logs ORDER BY unix_time DESC, id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def recent_actions(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self.get_recent_actions(limit=limit)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_events,
                        SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful_events,
                        SUM(CASE WHEN clog_detected = 1 THEN 1 ELSE 0 END) AS clogs_detected,
                        SUM(CASE WHEN success = 1 THEN portions ELSE 0 END) AS total_portions_dispensed,
                        MAX(timestamp) AS last_event_time
                    FROM feed_events
                    """
                ).fetchone()
                total = int(row["total_events"] or 0)
                succ = int(row["successful_events"] or 0)
                clogs = int(row["clogs_detected"] or 0)
                portions = int(row["total_portions_dispensed"] or 0)
                return {
                    "total_events": total,
                    "successful_events": succ,
                    "clogs_detected": clogs,
                    "total_portions_dispensed": portions,
                    "success_rate_pct": round((succ / total) * 100.0, 1) if total > 0 else 100.0,
                    "last_event_time": row["last_event_time"],
                }
            finally:
                conn.close()

    def portions_today(self, event_type: Optional[str] = None) -> int:
        with self._lock:
            conn = self._connect()
            try:
                if event_type:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(portions), 0) FROM feed_events "
                        "WHERE date(timestamp) = date('now', 'localtime') AND event_type = ?",
                        (event_type,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(portions), 0) FROM feed_events "
                        "WHERE date(timestamp) = date('now', 'localtime') AND success = 1"
                    ).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()

    def consecutive_missed_feeds(self, limit: int = 20) -> int:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT success, clog_detected, outcome FROM feed_events "
                    "WHERE outcome != 'bowl_check' ORDER BY unix_time DESC, id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            finally:
                conn.close()
        misses = 0
        for r in rows:
            if r["outcome"] == "dispensed":
                break
            if r["outcome"] in ("motor_no_kibble", "no_motor"):
                misses += 1
        return misses

    def record_food_evidence(self, source: str, detail: str = "") -> int:
        now_ts = int(time.time())
        iso_ts = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "INSERT INTO food_observations (timestamp, unix_time, source, detail) VALUES (?, ?, ?, ?)",
                        (iso_ts, now_ts, source, detail),
                    )
                    return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def last_food_evidence(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                obs = conn.execute(
                    "SELECT unix_time, timestamp, source, detail FROM food_observations "
                    "ORDER BY unix_time DESC, id DESC LIMIT 1"
                ).fetchone()
                ev = conn.execute(
                    "SELECT unix_time, timestamp, outcome FROM feed_events "
                    "WHERE success = 1 ORDER BY unix_time DESC, id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        candidates = []
        if obs:
            candidates.append(dict(obs))
        if ev:
            candidates.append(
                {
                    "unix_time": ev["unix_time"],
                    "timestamp": ev["timestamp"],
                    "source": ev["outcome"] or "feed_event",
                    "detail": "",
                }
            )
        if not candidates:
            return None
        return max(candidates, key=lambda c: c["unix_time"])

    def record_refill(self, capacity_portions: int, note: str = "") -> int:
        now_ts = int(time.time())
        iso_ts = datetime.fromtimestamp(now_ts).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    max_eid_row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM feed_events").fetchone()
                    after_eid = int(max_eid_row[0] or 0) if max_eid_row else 0
                    cur = conn.execute(
                        "INSERT INTO hopper_refills (timestamp, unix_time, capacity_portions, note, after_event_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (iso_ts, now_ts, max(1, int(capacity_portions)), note, after_eid),
                    )
                    row_id = int(cur.lastrowid or 0)
            finally:
                conn.close()
            self._perform_online_backup()
            return row_id

    def hopper_status(self, default_capacity: int = 240) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                refill = conn.execute(
                    "SELECT * FROM hopper_refills ORDER BY id DESC LIMIT 1"
                ).fetchone()
                since_iso = str(refill["timestamp"]) if refill else None
                since_id = int(refill["id"]) if refill else 0
                after_eid = int(refill["after_event_id"] or 0) if (refill and "after_event_id" in refill.keys()) else 0
                cap = int(refill["capacity_portions"]) if refill else int(default_capacity)
                base_cond = (
                    "(outcome = 'dispensed' "
                    "OR event_type = 'manual_feed_unverified' "
                    "OR (COALESCE(outcome, 'unknown') = 'unknown' AND (success = 1 OR motor_detected = 1)) "
                    "OR (success = 1 AND COALESCE(outcome, '') != 'motor_no_kibble'))"
                )
                if since_iso:
                    used_row = conn.execute(
                        f"""
                        SELECT COALESCE(SUM(portions), 0) FROM feed_events
                        WHERE {base_cond}
                          AND (
                                datetime(timestamp) > datetime(?)
                             OR (datetime(timestamp) = datetime(?) AND id > ?)
                          )
                        """,
                        (since_iso, since_iso, after_eid),
                    ).fetchone()
                else:
                    used_row = conn.execute(
                        f"SELECT COALESCE(SUM(portions), 0) FROM feed_events WHERE {base_cond}"
                    ).fetchone()
                used = int(used_row[0] or 0)
            finally:
                conn.close()
        left = max(0, cap - used)
        pct = round((left / cap) * 100.0, 1) if cap > 0 else 0.0
        return {
            "known": refill is not None,
            "refilled_at": refill["timestamp"] if refill else None,
            "refill_id": since_id or None,
            "capacity_portions": cap,
            "portions_used": used,
            "portions_left": left,
            "percent_left": pct,
        }

    def learned_baselines(self, min_samples: int = 3, window: int = 30) -> Dict[str, Optional[float]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT portions, motor_duration_sec, kibble_rate
                    FROM feed_events
                    WHERE success = 1 AND clog_detected = 0 AND motor_duration_sec > 0.5
                      AND (user_feedback IS NULL OR user_feedback = 'right')
                    ORDER BY unix_time DESC LIMIT ?
                    """,
                    (int(window),),
                ).fetchall()
            finally:
                conn.close()
        rates = sorted(float(r["kibble_rate"]) for r in rows if float(r["kibble_rate"]) > 0)
        sec_per_p = sorted(
            float(r["motor_duration_sec"]) / int(r["portions"])
            for r in rows
            if int(r["portions"]) > 0 and float(r["motor_duration_sec"]) > 0
        )
        return {
            "kibble_rate": rates[len(rates) // 2] if len(rates) >= min_samples else None,
            "motor_sec_per_portion": sec_per_p[len(sec_per_p) // 2] if len(sec_per_p) >= min_samples else None,
            "sample_count": len(rows),
        }

    def record_feedback(
        self, verdict: str, event_id: Optional[int] = None, note: str = ""
    ) -> Optional[Dict[str, Any]]:
        v = verdict.strip().lower()
        if v in ("ok", "correct", "good", "yes", "true", "right"):
            v = "right"
        elif v in ("bad", "false_alarm", "false", "no", "wrong", "mistake"):
            v = "wrong"
        else:
            raise ValueError("verdict must be 'right' or 'wrong'")
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    if event_id is None:
                        row = conn.execute(
                            "SELECT id FROM feed_events ORDER BY unix_time DESC, id DESC LIMIT 1"
                        ).fetchone()
                        if not row:
                            return None
                        event_id = int(row["id"])
                    conn.execute(
                        "UPDATE feed_events SET user_feedback = ?, feedback_note = ?, feedback_at = ? WHERE id = ?",
                        (v, note[:500], now_iso, int(event_id)),
                    )
                    updated = conn.execute("SELECT * FROM feed_events WHERE id = ?", (int(event_id),)).fetchone()
                    return dict(updated) if updated else None
            finally:
                conn.close()

    def feedback_labeled_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, portions, success, clog_detected, outcome, kibble_peaks,
                           motor_energy_db, impact_energy_db, motor_duration_sec, kibble_rate,
                           user_feedback
                    FROM feed_events
                    WHERE user_feedback IN ('right', 'wrong') AND motor_duration_sec > 0
                    ORDER BY unix_time DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def events_older_than(self, cutoff_unix: int | str, failed_only: Optional[bool] = None) -> List[Dict[str, Any]]:
        clause = ""
        if failed_only is True:
            clause = "AND (success = 0 OR clog_detected = 1)"
        elif failed_only is False:
            clause = "AND success = 1 AND clog_detected = 0"
        with self._lock:
            conn = self._connect()
            try:
                if isinstance(cutoff_unix, str):
                    rows = conn.execute(
                        f"SELECT * FROM feed_events WHERE files_purged = 0 AND datetime(timestamp) < datetime(?) {clause} "
                        "ORDER BY unix_time ASC, id ASC",
                        (cutoff_unix,),
                    ).fetchall()
                else:
                    c_int = int(cutoff_unix)
                    rows = conn.execute(
                        f"SELECT * FROM feed_events WHERE files_purged = 0 "
                        f"AND (unix_time < ? OR datetime(timestamp) < datetime(?, 'unixepoch', 'localtime')) {clause} "
                        "ORDER BY unix_time ASC, id ASC",
                        (c_int, c_int),
                    ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def events_with_files(self, oldest_first: bool = True) -> List[Dict[str, Any]]:
        order = "ASC" if oldest_first else "DESC"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM feed_events WHERE files_purged = 0 ORDER BY unix_time {order}, id {order}"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def mark_files_purged(self, event_id: int, exported: bool = False) -> None:
        now_iso = datetime.now().isoformat(timespec="seconds") if exported else None
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        UPDATE feed_events
                           SET before_image = NULL, after_image = NULL, annotated_image = NULL,
                               video_clip = NULL, audio_clip = NULL, files_purged = 1,
                               exported_at = COALESCE(?, exported_at)
                         WHERE id = ?
                        """,
                        (now_iso, int(event_id)),
                    )
            finally:
                conn.close()

    def clear_event_video(self, event_id: int) -> None:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("UPDATE feed_events SET video_clip = NULL WHERE id = ?", (int(event_id),))
            finally:
                conn.close()

    def delete_events_older_than(self, cutoff_unix: int | str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    if isinstance(cutoff_unix, str):
                        c1 = conn.execute(
                            "DELETE FROM feed_events WHERE datetime(timestamp) < datetime(?)",
                            (cutoff_unix,),
                        ).rowcount
                    else:
                        c_int = int(cutoff_unix)
                        c1 = conn.execute(
                            "DELETE FROM feed_events WHERE unix_time < ? OR datetime(timestamp) < datetime(?, 'unixepoch', 'localtime')",
                            (c_int, c_int),
                        ).rowcount
                        conn.execute("DELETE FROM action_logs WHERE unix_time < ?", (c_int,))
                        conn.execute("DELETE FROM food_observations WHERE unix_time < ?", (c_int,))
                    return int(c1 or 0)
            finally:
                conn.close()

    def daily_rollup(self, date_iso: Optional[str] = None) -> Dict[str, Any]:
        day = date_iso or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT event_type, portions, success, clog_detected, outcome, motor_duration_sec, kibble_rate "
                    "FROM feed_events WHERE substr(timestamp, 1, 10) = ? ORDER BY unix_time ASC",
                    (day,),
                ).fetchall()
            finally:
                conn.close()
        events = [dict(r) for r in rows]
        ok = [e for e in events if e["success"]]
        failed = [e for e in events if not e["success"] and e["event_type"] != "manual_check"]
        retries = [e for e in events if e["event_type"] == "auto_retry_verify"]
        return {
            "date": day,
            "meals_verified": len(ok),
            "meals_failed": len(failed),
            "clogs": sum(1 for e in events if e["clog_detected"]),
            "retries": len(retries),
            "portions_dispensed": sum(int(e["portions"]) for e in ok),
            "events": events,
        }
