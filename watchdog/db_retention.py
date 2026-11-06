"""Retention queries for KibbleDatabase (kept separate from the core logging methods).

Media deletion never deletes the metric row: path columns are nulled and `media_purged_at` is set.
Rows are only deleted by `delete_events_older_than` (DB_RETENTION_DAYS > 0).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

MEDIA_COLUMNS = ("video_clip", "audio_clip", "before_image", "after_image", "annotated_image")
_ANY_MEDIA = " OR ".join(f"{c} IS NOT NULL" for c in MEDIA_COLUMNS)


class RetentionQueriesMixin:
    """Requires `self._lock` and `self._connect()` from KibbleDatabase."""

    def _rows(self, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                return [dict(r) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    return conn.execute(sql, args).rowcount
            finally:
                conn.close()

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        rows = self._rows("SELECT * FROM feed_events WHERE id = ?", (event_id,))
        return rows[0] if rows else None

    def events_with_media(self) -> List[Dict[str, Any]]:
        return self._rows(f"SELECT * FROM feed_events WHERE media_purged_at IS NULL AND ({_ANY_MEDIA}) ORDER BY id")

    def events_older_than(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM feed_events WHERE timestamp < ? ORDER BY id", (cutoff_iso,))

    def delete_events_older_than(self, cutoff_iso: str) -> int:
        return self._write("DELETE FROM feed_events WHERE timestamp < ?", (cutoff_iso,))

    def ok_video_events_older_than(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        return self._rows(
            "SELECT * FROM feed_events WHERE success = 1 AND video_clip IS NOT NULL AND timestamp < ? "
            "AND COALESCE(outcome, '') IN ('dispensed', 'bowl_check')",
            (cutoff_iso,),
        )

    def mark_media_purged(self, event_id: int) -> None:
        cols = ", ".join(f"{c} = NULL" for c in MEDIA_COLUMNS)
        self._write(
            f"UPDATE feed_events SET {cols}, media_purged_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), event_id),
        )

    def clear_media_column(self, event_id: int, column: str) -> None:
        if column not in MEDIA_COLUMNS:
            raise ValueError(column)
        self._write(f"UPDATE feed_events SET {column} = NULL WHERE id = ?", (event_id,))

    def mark_ml_exported(self, event_id: int) -> None:
        self._write(
            "UPDATE feed_events SET ml_exported_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), event_id),
        )

    def referenced_media_paths(self) -> Set[str]:
        paths: Set[str] = set()
        for row in self.events_with_media():
            paths.update(row[c] for c in MEDIA_COLUMNS if row.get(c))
        return paths

    def media_owner_map(self) -> Dict[str, int]:
        owners: Dict[str, int] = {}
        for row in self.events_with_media():
            for c in MEDIA_COLUMNS:
                if row.get(c):
                    owners[row[c]] = row["id"]
        return owners
