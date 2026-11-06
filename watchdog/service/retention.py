"""Retention: keep structured metrics (SQLite + JSONL) for ML training, control how long footage stays on disk.

Policy (all knobs in .env, see `.env.example`):
- KEEP_FOOTAGE=false        media is deleted right after analysis (metrics stay in SQLite / JSONL)
- CAPTURE_RETENTION_DAYS    media of normal events older than N days is deleted (0 = keep forever)
- KEEP_FAILED_FOOTAGE_DAYS  longer keep for clog / failed events (falls back to CAPTURE_RETENTION_DAYS)
- VIDEO_KEEP_DAYS_OK        optional extra tier: only the MP4 of verified-good feeds after N days (0 = off)
- CAPTURE_MAX_MB            disk safety cap for the captures folder (0 = off), oldest media first
- DB_RETENTION_DAYS         0 = keep feed_events rows forever; >0 = delete older rows (after export)
- EXPORT_ML_ON_PRUNE        append each event's metrics to ML_EXPORT_PATH (JSONL) before anything is deleted

Deleting media never deletes the metric row: the media path columns are nulled and `media_purged_at` is set.
Only files inside the captures folder are ever deleted (review-lab clips in sub-folders are never touched).
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from watchdog.paths import PROJECT_ROOT

logger = logging.getLogger("poor_puppy.retention")

MEDIA_COLUMNS = ("video_clip", "audio_clip", "before_image", "after_image", "annotated_image")
FAILED_OUTCOMES = ("motor_no_kibble", "no_motor", "unverified", "recording_cut_short")


def ml_record(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Flat, training-friendly view of one feed event (numeric features + labels, media paths if any)."""
    details: Dict[str, Any] = {}
    try:
        details = json.loads(row.get("details_json") or "{}")
    except (TypeError, ValueError):
        pass
    audio = details.get("audio") or {}
    visual = details.get("visual") or {}
    return {
        "event_id": row.get("id"),
        "timestamp": row.get("timestamp"),
        "event_type": row.get("event_type"),
        "portions": row.get("portions"),
        "success": bool(row.get("success")),
        "clog_detected": bool(row.get("clog_detected")),
        "outcome": row.get("outcome"),
        "audio_state": row.get("audio_state"),
        "motor_detected": bool(row.get("motor_detected")),
        "motor_duration_sec": row.get("motor_duration_sec"),
        "kibble_peaks": row.get("kibble_peaks"),
        "kibble_rate": row.get("kibble_rate"),
        "ting_count": audio.get("ting_count"),
        "ting_rate_per_sec": audio.get("ting_rate_per_sec"),
        "motor_db": row.get("motor_db"),
        "impact_db": row.get("impact_db"),
        "before_fill_pct": row.get("before_fill_pct"),
        "after_fill_pct": row.get("after_fill_pct"),
        "fill_delta_pct": row.get("fill_delta_pct"),
        "bowl_empty": bool(row.get("bowl_empty")),
        "food_increased": bool(row.get("food_increased")),
        "food_seen_in_bowl": visual.get("food_present_cv"),
        "dog_in_bowl": bool(row.get("dog_in_bowl")),
        "infrared_mode": bool(row.get("infrared_mode")),
        "food_weight_g": row.get("food_weight_g"),
        "feed_abnormal": row.get("feed_abnormal"),
        "warnings": details.get("warnings"),
        "diagnosis": row.get("diagnosis"),
        "media": {c: row.get(c) for c in MEDIA_COLUMNS if row.get(c)},
        "export_reason": reason,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }


class RetentionManager:
    """Applies the retention policy to the captures folder and the feed_events table."""

    def __init__(self, cfg, db, work_dir: Path, protected: Iterable[Path] = ()):
        self.cfg = cfg
        self.db = db
        self.work_dir = Path(work_dir)
        self.protected = {Path(p).resolve() for p in protected}

    # ------------------------------------------------------------------ public API
    def run(self) -> Dict[str, int]:
        """One full pass (scheduled hourly + at startup)."""
        stats = {"events_purged": 0, "files_deleted": 0, "rows_deleted": 0, "exported": 0}
        for row in self.db.events_with_media():
            if self._media_expired(row):
                self._purge_event(row, "media_retention", stats)
        self._tier_ok_videos(stats)
        stats["files_deleted"] += self._prune_orphans()
        self._enforce_size_cap(stats)
        if self.cfg.db_retention_days > 0:
            cutoff = self._cutoff(self.cfg.db_retention_days)
            for row in self.db.events_older_than(cutoff):
                self._export(row, "db_retention", stats)
                self._delete_files(row)
            stats["rows_deleted"] = self.db.delete_events_older_than(cutoff)
        if any(stats.values()):
            logger.info("Retention pass: %s", stats)
        return stats

    def purge_after_analysis(self, event_id: int) -> None:
        """KEEP_FOOTAGE=false: drop an event's media right after it was analyzed and logged."""
        if self.cfg.keep_footage:
            return
        row = self.db.get_event(event_id)
        if row:
            self._purge_event(row, "keep_footage_false", {"events_purged": 0, "files_deleted": 0, "exported": 0})

    # ------------------------------------------------------------------ policy
    @staticmethod
    def _cutoff(days: float) -> str:
        return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    def _is_failed(self, row: Dict[str, Any]) -> bool:
        return bool(row.get("clog_detected")) or (
            not row.get("success") and (row.get("outcome") in FAILED_OUTCOMES or row.get("outcome") is None)
        )

    def _media_expired(self, row: Dict[str, Any]) -> bool:
        if not self.cfg.keep_footage:
            return True
        days = self.cfg.keep_failed_footage_days if self._is_failed(row) else self.cfg.capture_retention_days
        return days > 0 and str(row.get("timestamp", "")) < self._cutoff(days)

    # ------------------------------------------------------------------ actions
    def _purge_event(self, row: Dict[str, Any], reason: str, stats: Dict[str, int]) -> None:
        self._export(row, reason, stats)
        stats["files_deleted"] += self._delete_files(row)
        self.db.mark_media_purged(row["id"])
        stats["events_purged"] += 1

    def _export(self, row: Dict[str, Any], reason: str, stats: Dict[str, int]) -> None:
        if not self.cfg.export_ml_on_prune or row.get("ml_exported_at"):
            return
        path = Path(self.cfg.ml_export_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ml_record(row, reason), default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.db.mark_ml_exported(row["id"])
        stats["exported"] = stats.get("exported", 0) + 1

    def _safe(self, path: Optional[str]) -> Optional[Path]:
        """Only regular files directly inside the captures folder, never protected files."""
        if not path:
            return None
        p = Path(path)
        try:
            rp = p.resolve()
            if rp.parent != self.work_dir.resolve() or rp in self.protected or not p.is_file():
                return None
        except OSError:
            return None
        return p

    def _delete_files(self, row: Dict[str, Any], columns: Iterable[str] = MEDIA_COLUMNS) -> int:
        n = 0
        for col in columns:
            p = self._safe(row.get(col))
            if p is not None:
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    def _tier_ok_videos(self, stats: Dict[str, int]) -> None:
        """Optional: only the MP4 of verified-good feeds after VIDEO_KEEP_DAYS_OK (photos/audio stay)."""
        if self.cfg.video_keep_days_ok <= 0 or not self.cfg.keep_footage:
            return
        for row in self.db.ok_video_events_older_than(self._cutoff(self.cfg.video_keep_days_ok)):
            if self._delete_files(row, ("video_clip",)):
                self.db.clear_media_column(row["id"], "video_clip")
                stats["files_deleted"] += 1

    def _media_files(self) -> List[Path]:
        return [
            p for p in self.work_dir.glob("*")
            if p.is_file() and p.resolve() not in self.protected and p.suffix.lower() in (".mp4", ".wav", ".jpg", ".jpeg")
        ]

    def _prune_orphans(self) -> int:
        """Files no event references (periodic food/eaten-check snapshots, leftovers)."""
        days = self.cfg.capture_retention_days if self.cfg.keep_footage else 1.0
        if days <= 0:
            return 0
        referenced = self.db.referenced_media_paths()
        now, n = time.time(), 0
        for p in self._media_files():
            if str(p) in referenced or str(p.resolve()) in referenced:
                continue
            if now - p.stat().st_mtime > days * 86400:
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
        return n

    def _enforce_size_cap(self, stats: Dict[str, int]) -> None:
        """Disk safety: oldest media first until the folder is under CAPTURE_MAX_MB (0 = off)."""
        cap = int(self.cfg.capture_max_mb) * 1024 * 1024
        if cap <= 0:
            return

        def folder_bytes() -> int:
            return sum(f.stat().st_size for f in self._media_files())

        if folder_bytes() <= cap:
            return
        owners = self.db.media_owner_map()
        for p in sorted(self._media_files(), key=lambda f: f.stat().st_mtime):
            if folder_bytes() <= cap:
                break
            if not p.exists():
                continue  # already removed together with its event
            event_id = owners.get(str(p)) or owners.get(str(p.resolve()))
            row = self.db.get_event(event_id) if event_id else None
            if row:
                self._purge_event(row, "size_cap", stats)
                continue
            try:
                p.unlink()
                stats["files_deleted"] += 1
            except OSError:
                pass
        logger.warning("Captures folder exceeded CAPTURE_MAX_MB=%d; oldest media removed", self.cfg.capture_max_mb)
