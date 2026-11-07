"""Capture & database retention manager (`RetentionManager`) with ML JSONL export."""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from custom_components.molly_cody_feeder.core.config import RuntimeConfig
from custom_components.molly_cody_feeder.core.db import KibbleDatabase

logger = logging.getLogger("molly_cody_feeder.retention")

_FILE_COLS = ("before_image", "after_image", "annotated_image", "video_clip", "audio_clip")
_SEC_PER_DAY = 86400

_EXPORT_FIELDS = (
    "id", "timestamp", "unix_time", "event_type", "mode", "transport", "portions",
    "success", "clog_detected", "outcome", "audio_state", "motor_detected",
    "motor_duration_sec", "kibble_peaks", "kibble_rate", "motor_energy_db",
    "impact_energy_db", "before_fill_ratio", "after_fill_ratio", "fill_delta_ratio",
    "frame_diff_ratio", "is_infrared", "dog_at_bowl", "pre_weight_g", "post_weight_g",
    "feed_abnormal", "diagnosis", "user_feedback", "feedback_note", "feedback_at",
)


class RetentionManager:
    """Prunes captured media files on disk while preserving SQLite metrics and exporting ML JSONL."""

    def __init__(
        self,
        cfg: RuntimeConfig,
        db: KibbleDatabase,
        work_dir: Path,
        protected: Iterable[Path] = (),
    ):
        self.cfg = cfg
        self.db = db
        self.work_dir = work_dir.resolve()
        self.protected: Set[Path] = {p.resolve() for p in protected}

    def after_event(self, event_id: int, passed: bool) -> Dict[str, int]:
        if not self.cfg.keep_footage:
            row = self._get_event(event_id)
            if row:
                exp = self._export_row(row) if self.cfg.export_ml_on_prune else False
                n = self._delete_files(row)
                self.db.mark_files_purged(event_id, exported=exp)
                return {"events_purged": 1, "files_deleted": n}
        return self.run()

    def prune(self) -> Dict[str, int]:
        return self.run()

    def run(self) -> Dict[str, int]:
        now = int(time.time())
        purged_events = 0
        deleted_files = 0
        exported = 0

        if not self.cfg.keep_footage:
            for row in self.db.events_with_files():
                e, f = self._purge_row(row)
                purged_events += 1
                deleted_files += f
                exported += int(e)
        else:
            ok_days = self.cfg.capture_retention_days
            fail_days = max(ok_days, self.cfg.keep_failed_footage_days) if ok_days > 0 else self.cfg.keep_failed_footage_days

            if (
                0 < self.cfg.video_keep_days_ok < ok_days
                or (ok_days == 0 and self.cfg.video_keep_days_ok > 0)
            ):
                v_cut = now - int(self.cfg.video_keep_days_ok * _SEC_PER_DAY)
                for row in self.db.events_older_than(v_cut, failed_only=False):
                    vp = row.get("video_clip")
                    if vp and self._unlink(vp):
                        deleted_files += 1
                        self.db.clear_event_video(int(row["id"]))

            if ok_days > 0:
                cut_ok = now - int(ok_days * _SEC_PER_DAY)
                for row in self.db.events_older_than(cut_ok, failed_only=False):
                    e, f = self._purge_row(row)
                    purged_events += 1
                    deleted_files += f
                    exported += int(e)

            if fail_days > 0:
                cut_fail = now - int(fail_days * _SEC_PER_DAY)
                for row in self.db.events_older_than(cut_fail, failed_only=True):
                    e, f = self._purge_row(row)
                    purged_events += 1
                    deleted_files += f
                    exported += int(e)

        if self.cfg.capture_max_mb > 0:
            cap_bytes = int(self.cfg.capture_max_mb * 1024 * 1024)
            pe, df, ex = self._enforce_size_cap(cap_bytes)
            purged_events += pe
            deleted_files += df
            exported += ex

        rows_deleted = 0
        if self.cfg.db_retention_days > 0:
            cut_db = now - int(self.cfg.db_retention_days * _SEC_PER_DAY)
            for row in self.db.events_older_than(cut_db):
                e, f = self._purge_row(row)
                deleted_files += f
                exported += int(e)
            rows_deleted = self.db.delete_events_older_than(cut_db)

        self._remove_empty_dirs()
        stats = {
            "events_purged": purged_events,
            "files_deleted": deleted_files,
            "rows_deleted": rows_deleted,
            "exported": exported,
        }
        if any(stats.values()):
            logger.info("Retention pass: %s", stats)
        return stats

    def export_all_for_ml(self, out_path: Optional[str] = None) -> Dict[str, Any]:
        target = Path(out_path or self.cfg.ml_export_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.db.get_recent_events(limit=100000)
        count = 0
        with target.open("w", encoding="utf-8") as fh:
            for row in reversed(rows):
                fh.write(json.dumps(self._ml_record(row), default=str) + "\n")
                count += 1
        return {"ok": True, "path": str(target), "records": count}

    def status(self) -> Dict[str, Any]:
        used = self._dir_bytes(self.work_dir)
        ml_p = Path(self.cfg.ml_export_path)
        return {
            "keep_footage": self.cfg.keep_footage,
            "capture_retention_days": self.cfg.capture_retention_days,
            "keep_failed_footage_days": self.cfg.keep_failed_footage_days,
            "video_keep_days_ok": self.cfg.video_keep_days_ok,
            "capture_max_mb": self.cfg.capture_max_mb,
            "db_retention_days": self.cfg.db_retention_days,
            "captures_mb": round(used / (1024 * 1024), 2),
            "ml_export_path": str(ml_p),
            "ml_export_kb": round(ml_p.stat().st_size / 1024, 1) if ml_p.exists() else 0.0,
        }

    def _get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        for r in self.db.get_recent_events(limit=50):
            if int(r["id"]) == int(event_id):
                return r
        return None

    def _purge_row(self, row: Dict[str, Any]) -> "tuple[bool, int]":
        exp = False
        if self.cfg.export_ml_on_prune and not row.get("exported_at"):
            exp = self._export_row(row)
        n = self._delete_files(row)
        self.db.mark_files_purged(int(row["id"]), exported=exp)
        return exp, n

    def _export_row(self, row: Dict[str, Any]) -> bool:
        try:
            p = Path(self.cfg.ml_export_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(self._ml_record(row), default=str) + "\n")
            return True
        except Exception as exc:
            logger.warning("ML JSONL export failed for event #%s: %s", row.get("id"), exc)
            return False

    @staticmethod
    def _ml_record(row: Dict[str, Any]) -> Dict[str, Any]:
        rec = {k: row.get(k) for k in _EXPORT_FIELDS}
        rec["event_id"] = row.get("id")
        try:
            raw = json.loads(row.get("raw_json") or "{}")
            rec["audio"] = raw.get("audio")
            rec["visual"] = raw.get("visual")
            rec["anomaly"] = raw.get("anomaly")
        except Exception:
            pass
        rec["exported_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return rec

    def _delete_files(self, row: Dict[str, Any]) -> int:
        deleted = 0
        dirs: Set[Path] = set()
        for col in _FILE_COLS:
            val = row.get(col)
            if not val:
                continue
            p = self._safe(val)
            if p is None:
                continue
            dirs.add(p.parent)
            if self._unlink(str(p)):
                deleted += 1
        for d in dirs:
            if d != self.work_dir and d.exists():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
        return deleted

    def _safe(self, path_str: str) -> Optional[Path]:
        try:
            p = Path(path_str).resolve()
        except Exception:
            return None
        if p in self.protected:
            return None
        if self.work_dir == p or self.work_dir not in p.parents:
            return None
        return p

    def _unlink(self, path_str: str) -> bool:
        p = self._safe(path_str)
        if p is None or not p.exists():
            return False
        try:
            p.unlink()
            return True
        except OSError as exc:
            logger.debug("Could not remove %s: %s", p, exc)
            return False

    def _enforce_size_cap(self, cap_bytes: int) -> "tuple[int, int, int]":
        used = self._dir_bytes(self.work_dir)
        if used <= cap_bytes:
            return 0, 0, 0
        rows: List[Dict[str, Any]] = self.db.events_with_files(oldest_first=True)
        ordered = [r for r in rows if r["success"] and not r["clog_detected"]] + [
            r for r in rows if not r["success"] or r["clog_detected"]
        ]
        pe = df = ex = 0
        for row in ordered:
            if self._dir_bytes(self.work_dir) <= cap_bytes:
                break
            e, f = self._purge_row(row)
            pe += 1
            df += f
            ex += int(e)
        if self._dir_bytes(self.work_dir) > cap_bytes:
            for sub in sorted(self.work_dir.glob("feed_*"), key=lambda p: p.stat().st_mtime):
                if self._dir_bytes(self.work_dir) <= cap_bytes:
                    break
                if sub.is_dir():
                    shutil.rmtree(sub, ignore_errors=True)
                    df += 1
        logger.warning("Captures folder exceeded CAPTURE_MAX_MB=%d; oldest media removed", self.cfg.capture_max_mb)
        return pe, df, ex

    def _remove_empty_dirs(self) -> None:
        if not self.work_dir.exists():
            return
        for sub in self.work_dir.glob("feed_*"):
            if sub.is_dir():
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    pass

    def _dir_bytes(self, root: Path) -> int:
        if not root.exists():
            return 0
        total = 0
        for p in root.rglob("*"):
            if p.is_file() and p.resolve() not in self.protected:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total
