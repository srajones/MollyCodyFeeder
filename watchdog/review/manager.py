"""ReviewManager: recorded clips, ground-truth labels and live test captures for the Review Lab."""

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from watchdog.analysis import BowlAnalyzer
from watchdog.config import WatchdogConfig
from watchdog.db import KibbleDatabase
from watchdog.molly import MollyFeederClient
from watchdog.paths import PROJECT_ROOT

logger = logging.getLogger("poor_puppy.review")


CLIPS_DIR = PROJECT_ROOT / "captures" / "review_clips"


LABELS_JSON = CLIPS_DIR / "training_labels.json"


class ReviewManager:
    def __init__(self):
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = WatchdogConfig()
        self.db = KibbleDatabase(self.cfg.db_path)
        self.analyzer = BowlAnalyzer(self.cfg)
        self.client = MollyFeederClient(
            email=self.cfg.molly_user,
            password=self.cfg.molly_pass,
            mode="cloud",
            device_id=self.cfg.tuya_device_id,
            local_ip=self.cfg.feeder_local_ip,
            local_key=self.cfg.feeder_local_key,
        )
        self.lock = threading.RLock()
        self.busy_status: str = ""
        self._init_labels_table()
        self.reanalyze_all_clips()

    def reanalyze_all_clips(self) -> None:
        """Re-computes audio and visual metrics on all stored clips using the latest BowlAnalyzer."""
        for rec in self.list_clips():
            cid = rec["clip_id"]
            metrics = dict(rec.get("metrics") or {})
            wav_p = CLIPS_DIR / f"{cid}.wav"
            aft_p = CLIPS_DIR / f"{cid}_after.jpg"
            bef_p = CLIPS_DIR / f"{cid}_before.jpg"
            ann_p = CLIPS_DIR / f"{cid}_annotated.jpg"
            if wav_p.exists():
                try:
                    ares = self.analyzer.analyze_dispense_audio(str(wav_p))
                    metrics["audio"] = ares.__dict__
                except Exception as exc:
                    logger.warning("Re-analyze audio failed for %s: %s", cid, exc)
            if aft_p.exists():
                try:
                    vres = self.analyzer.analyze_bowl_visual(
                        after_image_path=str(aft_p),
                        before_image_path=str(bef_p) if bef_p.exists() else None,
                        annotated_output_path=str(ann_p),
                    )
                    vdict = dict(vres.__dict__)
                    vdict.pop("annotated_image_path", None)
                    metrics["visual"] = vdict
                except Exception as exc:
                    logger.warning("Re-analyze visual failed for %s: %s", cid, exc)
            rec["metrics"] = metrics
            self._save_clip_record(rec)

    def clear_all_clips(self) -> None:
        with self.db._lock:
            conn = self.db._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM training_labels")
                self.db._online_backup(conn)
            finally:
                conn.close()
        for f in CLIPS_DIR.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass
        self._export_labels_json()

    def _init_labels_table(self) -> None:
        with self.db._lock:
            conn = self.db._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS training_labels (
                            clip_id TEXT PRIMARY KEY,
                            updated_at TEXT NOT NULL,
                            title TEXT,
                            dispense_triggered INTEGER DEFAULT 0,
                            user_reviewed INTEGER DEFAULT 0,
                            feed_success INTEGER DEFAULT 0,
                            is_clog INTEGER DEFAULT 0,
                            heard_motor INTEGER DEFAULT 0,
                            heard_kibble INTEGER DEFAULT 0,
                            saw_kibble_accumulate INTEGER DEFAULT 0,
                            bowl_had_food_before INTEGER DEFAULT 0,
                            dog_at_bowl INTEGER DEFAULT 0,
                            notes TEXT DEFAULT '',
                            metrics_json TEXT DEFAULT '{}',
                            files_json TEXT DEFAULT '{}'
                        );
                        """
                    )
                self.db._online_backup(conn)
            finally:
                conn.close()

    def _save_clip_record(self, rec: Dict[str, Any]) -> None:
        with self.db._lock:
            conn = self.db._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO training_labels (
                            clip_id, updated_at, title, dispense_triggered, user_reviewed,
                            feed_success, is_clog, heard_motor, heard_kibble,
                            saw_kibble_accumulate, bowl_had_food_before, dog_at_bowl,
                            notes, metrics_json, files_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(clip_id) DO UPDATE SET
                            updated_at=excluded.updated_at,
                            title=excluded.title,
                            dispense_triggered=excluded.dispense_triggered,
                            user_reviewed=excluded.user_reviewed,
                            feed_success=excluded.feed_success,
                            is_clog=excluded.is_clog,
                            heard_motor=excluded.heard_motor,
                            heard_kibble=excluded.heard_kibble,
                            saw_kibble_accumulate=excluded.saw_kibble_accumulate,
                            bowl_had_food_before=excluded.bowl_had_food_before,
                            dog_at_bowl=excluded.dog_at_bowl,
                            notes=excluded.notes,
                            metrics_json=excluded.metrics_json,
                            files_json=excluded.files_json
                        """,
                        (
                            rec["clip_id"],
                            datetime.now().isoformat(timespec="seconds"),
                            rec.get("title", rec["clip_id"]),
                            1 if rec.get("dispense_triggered") else 0,
                            1 if rec.get("user_reviewed") else 0,
                            1 if rec.get("feed_success") else 0,
                            1 if rec.get("is_clog") else 0,
                            1 if rec.get("heard_motor") else 0,
                            1 if rec.get("heard_kibble") else 0,
                            1 if rec.get("saw_kibble_accumulate") else 0,
                            1 if rec.get("bowl_had_food_before") else 0,
                            1 if rec.get("dog_at_bowl") else 0,
                            rec.get("notes", ""),
                            json.dumps(rec.get("metrics", {})),
                            json.dumps(rec.get("files", {})),
                        ),
                    )
                self.db._online_backup(conn)
            finally:
                conn.close()
        self._export_labels_json()

    def _export_labels_json(self) -> None:
        all_clips = self.list_clips()
        tmp = LABELS_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(all_clips, indent=2), encoding="utf-8")
        os.replace(tmp, LABELS_JSON)

    def list_clips(self) -> List[Dict[str, Any]]:
        with self.db._lock:
            conn = self.db._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM training_labels ORDER BY clip_id DESC"
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["dispense_triggered"] = bool(d["dispense_triggered"])
                    d["user_reviewed"] = bool(d["user_reviewed"])
                    d["feed_success"] = bool(d["feed_success"])
                    d["is_clog"] = bool(d["is_clog"])
                    d["heard_motor"] = bool(d["heard_motor"])
                    d["heard_kibble"] = bool(d["heard_kibble"])
                    d["saw_kibble_accumulate"] = bool(d["saw_kibble_accumulate"])
                    d["bowl_had_food_before"] = bool(d["bowl_had_food_before"])
                    d["dog_at_bowl"] = bool(d["dog_at_bowl"])
                    d["metrics"] = json.loads(d.pop("metrics_json", "{}") or "{}")
                    d["files"] = json.loads(d.pop("files_json", "{}") or "{}")
                    out.append(d)
                return out
            finally:
                conn.close()

    def get_clip(self, clip_id: str) -> Dict[str, Any] | None:
        for c in self.list_clips():
            if c["clip_id"] == clip_id:
                return c
        return None

    def register_bundle(
        self,
        clip_id: str,
        title: str,
        dispense_triggered: bool,
        mp4_src: Path | None,
        wav_src: Path | None,
        before_src: Path | None,
        after_src: Path | None,
        extra_metrics: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        existing = self.get_clip(clip_id) or {}
        files: Dict[str, str] = {}

        if mp4_src and mp4_src.exists():
            dst = CLIPS_DIR / f"{clip_id}.mp4"
            if mp4_src.resolve() != dst.resolve():
                shutil.copy2(mp4_src, dst)
            files["mp4"] = f"/clips/{dst.name}"

        if wav_src and wav_src.exists():
            dst = CLIPS_DIR / f"{clip_id}.wav"
            if wav_src.resolve() != dst.resolve():
                shutil.copy2(wav_src, dst)
            files["wav"] = f"/clips/{dst.name}"

        # Ensure the MP4 has the WAV audio track muxed in for one-tap mobile playback
        if "mp4" in files and "wav" in files:
            mp4_file = CLIPS_DIR / f"{clip_id}.mp4"
            wav_file = CLIPS_DIR / f"{clip_id}.wav"
            mux_flag = CLIPS_DIR / f".{clip_id}.muxed"
            if not mux_flag.exists():
                import subprocess
                tmp_mux = CLIPS_DIR / f"{clip_id}.mux.mp4"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(mp4_file),
                        "-i", str(wav_file),
                        "-map", "0:v:0",
                        "-map", "1:a:0",
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-b:a", "64k",
                        "-shortest",
                        str(tmp_mux),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if tmp_mux.exists() and tmp_mux.stat().st_size > 1000:
                    os.replace(tmp_mux, mp4_file)
                    mux_flag.touch()

        if before_src and before_src.exists():
            dst = CLIPS_DIR / f"{clip_id}_before.jpg"
            if before_src.resolve() != dst.resolve():
                shutil.copy2(before_src, dst)
            view_dst = CLIPS_DIR / f"{clip_id}_before_bright.jpg"
            self._brighten_bowl_photo(dst, view_dst)
            files["before_jpg"] = f"/clips/{view_dst.name if view_dst.exists() else dst.name}"

        if after_src and after_src.exists():
            dst = CLIPS_DIR / f"{clip_id}_after.jpg"
            if after_src.resolve() != dst.resolve():
                shutil.copy2(after_src, dst)
            view_dst = CLIPS_DIR / f"{clip_id}_after_bright.jpg"
            self._brighten_bowl_photo(dst, view_dst)
            files["after_jpg"] = f"/clips/{view_dst.name if view_dst.exists() else dst.name}"

        metrics: Dict[str, Any] = dict(extra_metrics or {})
        if "wav" in files:
            try:
                ares = self.analyzer.analyze_dispense_audio(str(CLIPS_DIR / f"{clip_id}.wav"))
                metrics["audio"] = ares.__dict__
            except Exception as exc:
                logger.warning("Audio analysis error for %s: %s", clip_id, exc)

        if "after_jpg" in files:
            try:
                ann_dst = CLIPS_DIR / f"{clip_id}_annotated.jpg"
                vres = self.analyzer.analyze_bowl_visual(
                    after_image_path=str(CLIPS_DIR / f"{clip_id}_after.jpg"),
                    before_image_path=str(CLIPS_DIR / f"{clip_id}_before.jpg") if "before_jpg" in files else None,
                    annotated_output_path=str(ann_dst),
                )
                if ann_dst.exists():
                    files["annotated_jpg"] = f"/clips/{ann_dst.name}"
                vdict = dict(vres.__dict__)
                vdict.pop("annotated_image_path", None)
                metrics["visual"] = vdict
            except Exception as exc:
                logger.warning("Visual analysis error for %s: %s", clip_id, exc)

        rec = {
            "clip_id": clip_id,
            "title": title,
            "dispense_triggered": dispense_triggered,
            "user_reviewed": existing.get("user_reviewed", False),
            "feed_success": existing.get("feed_success", False),
            "is_clog": existing.get("is_clog", False),
            "heard_motor": existing.get("heard_motor", False),
            "heard_kibble": existing.get("heard_kibble", False),
            "saw_kibble_accumulate": existing.get("saw_kibble_accumulate", False),
            "bowl_had_food_before": existing.get("bowl_had_food_before", False),
            "dog_at_bowl": existing.get("dog_at_bowl", False),
            "notes": existing.get("notes", ""),
            "metrics": metrics,
            "files": files,
        }
        self._save_clip_record(rec)
        return rec

    @staticmethod
    def _brighten_bowl_photo(src_jpg: Path, dst_jpg: Path) -> None:
        try:
            import cv2
            import numpy as np

            img = cv2.imread(str(src_jpg))
            if img is None:
                return
            h, w = img.shape[:2]
            y_start = int(h * 0.70)
            bottom = img[y_start:, :, :]
            lut = np.array([((i / 255.0) ** 0.42) * 255.0 for i in range(256)], dtype=np.uint8)
            lifted = cv2.LUT(bottom, lut)
            lab = cv2.cvtColor(lifted, cv2.COLOR_BGR2LAB)
            clahe = cv2.createCLAHE(clipLimit=3.2, tileGridSize=(6, 6))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            ramp_len = max(1, int(h * 0.08))
            alpha = np.ones((h - y_start, 1, 1), dtype=np.float32)
            alpha[:ramp_len, 0, 0] = np.linspace(0.0, 1.0, ramp_len, dtype=np.float32)
            img[y_start:, :, :] = np.clip(
                bottom.astype(np.float32) * (1.0 - alpha) + enhanced.astype(np.float32) * alpha,
                0,
                255,
            ).astype(np.uint8)
            cv2.imwrite(str(dst_jpg), img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        except Exception:
            pass

    def trigger_new_capture(self, dispense_portions: int = 0, duration_sec: float = 11.0) -> Dict[str, Any]:
        with self.lock:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            kind = f"feed_{dispense_portions}p" if dispense_portions > 0 else "check_nodispense"
            clip_id = f"{ts}_{kind}"
            self.busy_status = (
                f"Recording {duration_sec:.0f}s Cloud HD Video + Audio "
                f"({'dispensing ' + str(dispense_portions) + ' portion(s)' if dispense_portions > 0 else 'no dispense'})..."
            )
        try:
            mp4_p = CLIPS_DIR / f"{clip_id}.mp4"
            wav_p = CLIPS_DIR / f"{clip_id}.wav"
            bef_p = CLIPS_DIR / f"{clip_id}_before.jpg"
            aft_p = CLIPS_DIR / f"{clip_id}_after.jpg"

            pre_st = self.client.get_status()
            disp_res = {"ok": False, "transport": "none"}

            def _trigger():
                if dispense_portions > 0:
                    ok, tr = self.client.dispense_food(dispense_portions)
                    disp_res["ok"] = ok
                    disp_res["transport"] = tr

            cap_res = self.client.capture_av_clip(
                duration_sec=duration_sec,
                output_mp4=str(mp4_p),
                output_wav=str(wav_p),
                before_jpg=str(bef_p),
                after_jpg=str(aft_p),
                trigger_fn=_trigger if dispense_portions > 0 else None,
                trigger_delay_sec=0.5,  # recording auto-extends, so no long settle wait needed
            )
            post_st = self.client.get_status()
            title = (
                f"Live Feed ({dispense_portions} Portion{'s' if dispense_portions != 1 else ''}) @ {ts}"
                if dispense_portions > 0
                else f"Baseline Check (No Dispense) @ {ts}"
            )
            return self.register_bundle(
                clip_id=clip_id,
                title=title,
                dispense_triggered=dispense_portions > 0,
                mp4_src=mp4_p,
                wav_src=wav_p,
                before_src=bef_p,
                after_src=aft_p,
                extra_metrics={
                    "capture": cap_res,
                    "pre_weight_g": pre_st.get("food_weight_g"),
                    "post_weight_g": post_st.get("food_weight_g"),
                    "feed_abnormal": post_st.get("feed_abnormal"),
                },
            )
        finally:
            with self.lock:
                self.busy_status = ""
