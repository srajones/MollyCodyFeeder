"""Single-run feed verification pipeline (`FeedVerifier`)."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from custom_components.molly_cody_feeder.core.alerter import HouseholdAlerter
from custom_components.molly_cody_feeder.core.analysis import (
    AudioAnalysisResult,
    BowlAnalyzer,
    VisualAnalysisResult,
    assess_dispense,
    feedback_suggests_false_alarm,
)
from custom_components.molly_cody_feeder.core.config import RuntimeConfig
from custom_components.molly_cody_feeder.core.db import KibbleDatabase
from custom_components.molly_cody_feeder.core.molly import MollyFeederClient
from custom_components.molly_cody_feeder.core.service.decision import (
    CAMERA_UNAVAILABLE,
    DISPENSED,
    MOTOR_NO_KIBBLE,
    RECORDING_CUT_SHORT,
    classify,
    decide_passed,
    is_clog,
    is_unverified,
    recording_cut_short,
    status_label,
)
from custom_components.molly_cody_feeder.core.service.models import VerifyRequest

logger = logging.getLogger("molly_cody_feeder.verifier")


class FeedVerifier:
    """Runs one feed verification pass and persists results to SQLite."""

    def __init__(
        self,
        cfg: RuntimeConfig,
        molly: MollyFeederClient,
        analyzer: BowlAnalyzer,
        db: KibbleDatabase,
        alerter: HouseholdAlerter,
        work_dir: Path,
        latest_snapshot: Path,
        schedule_eaten_check: Optional[Callable[[float], None]] = None,
        on_event_stored: Optional[Callable[[int, bool], None]] = None,
    ):
        self.cfg = cfg
        self.molly = molly
        self.analyzer = analyzer
        self.db = db
        self.alerter = alerter
        self.work_dir = work_dir
        self.latest_snapshot = latest_snapshot
        self.schedule_eaten_check = schedule_eaten_check
        self.on_event_stored = on_event_stored

    def run(self, req: VerifyRequest) -> Tuple[bool, Dict[str, Any]]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        event_dir = self.work_dir / f"feed_{ts}"
        event_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Starting feed verification (mode=%s, trigger_dispense=%s, portions=%d)...",
            self.molly.mode,
            req.trigger_dispense,
            req.portions,
        )

        pre = self.molly.get_status()
        pre_weight = pre.get("food_weight_g")
        feed_abnormal = int(pre.get("feed_abnormal") or 0)

        before_jpg = event_dir / "before.jpg"
        after_jpg = event_dir / "after.jpg"
        annotated_jpg = event_dir / "annotated_bowl.jpg"
        clip_mp4 = event_dir / "dispense.mp4"
        clip_wav = event_dir / "dispense.wav"

        audio_res, visual_res, session_info = self._capture_and_analyze(
            req, before_jpg, after_jpg, annotated_jpg, clip_mp4, clip_wav
        )
        cut_short = recording_cut_short(
            session_info,
            min_coverage=getattr(self.cfg, "recording_min_coverage", 0.75),
            max_tail_gap_sec=getattr(self.cfg, "recording_max_tail_gap_sec", 5.0),
        )
        post = self.molly.get_status()
        post_weight = post.get("food_weight_g")
        feed_abnormal = max(feed_abnormal, int(post.get("feed_abnormal") or 0))
        active_transport = str(post.get("active_transport", self.molly.mode))

        passed = self._decide(audio_res, visual_res, feed_abnormal, req.expect_dispense)
        fb_note = self._check_feedback_override(passed, req.expect_dispense, audio_res)
        if fb_note:
            passed = True

        outcome, passed = classify(
            audio_res, visual_res, req.expect_dispense, passed, cut_short=cut_short
        )
        anomaly = assess_dispense(
            audio_res,
            req.portions,
            self.db.learned_baselines(),
            weak_flow_ting_rate=getattr(self.cfg, "weak_flow_ting_rate", 4.6),
            recording_truncated=cut_short,
        )
        summary = self._build_summary(
            audio_res, visual_res, pre_weight, post_weight, feed_abnormal, fb_note, anomaly["note"],
            session_info=session_info if cut_short else None,
        )
        clog = is_clog(outcome, passed)
        state = self._build_state(
            ts, req, active_transport, passed, clog, outcome, pre_weight, post_weight,
            feed_abnormal, audio_res, visual_res, session_info, anomaly, summary,
            before_jpg, after_jpg, annotated_jpg, clip_mp4, clip_wav,
            cut_short=cut_short,
        )
        db_id = self._persist(state, req, active_transport, passed, clog, outcome, audio_res, visual_res,
                              pre_weight, post_weight, feed_abnormal, summary,
                              before_jpg, after_jpg, annotated_jpg, clip_mp4, clip_wav)
        state["db_event_id"] = db_id
        if self.on_event_stored:
            self.on_event_stored(db_id, passed)

        self._post_process(
            passed, outcome, req, state, summary, audio_res, visual_res,
            anomaly, annotated_jpg, clip_mp4, clip_wav, db_id,
        )
        return passed, state

    def _capture_and_analyze(
        self,
        req: VerifyRequest,
        before_jpg: Path,
        after_jpg: Path,
        annotated_jpg: Path,
        clip_mp4: Path,
        clip_wav: Path,
    ) -> Tuple[Optional[AudioAnalysisResult], Optional[VisualAnalysisResult], Dict[str, Any]]:
        session_info: Dict[str, Any] = {}
        audio_res: Optional[AudioAnalysisResult] = None
        vision_enabled = bool(getattr(self.cfg, "enable_vision_verification", True))
        mp4_enabled = bool(getattr(self.cfg, "enable_mp4_recording", True))
        audio_only_mode = bool(req.expect_dispense and not vision_enabled and not mp4_enabled)

        if self.cfg.camera_stream_url:
            session_info = self._capture_rtsp(req, before_jpg, after_jpg, clip_mp4)
            if clip_mp4.exists():
                audio_res = self.analyzer.analyze_dispense_audio(str(clip_mp4))
        else:
            def _trigger() -> None:
                if req.trigger_dispense:
                    logger.info("Triggering live dispense of %d portion(s) via %s...", req.portions, self.molly.mode)
                    self.molly.dispense_food(req.portions)

            rec_dur = 0.0
            if req.expect_dispense:
                min_for_portions = int(req.portions * 2.6 + 8)
                rec_dur = float(max(self.cfg.record_duration_seconds, min(75, min_for_portions)))
            clip_kwargs: Dict[str, Any] = {
                "duration_sec": rec_dur,
                "output_mp4": str(clip_mp4) if (req.expect_dispense and mp4_enabled) else "",
                "output_wav": str(clip_wav) if req.expect_dispense else "",
                "before_jpg": str(before_jpg) if (req.expect_dispense and vision_enabled) else None,
                "after_jpg": str(after_jpg) if vision_enabled else None,
                "trigger_fn": _trigger if req.trigger_dispense else None,
                "trigger_delay_sec": 1.0,
            }
            if audio_only_mode:
                clip_kwargs["decode_video"] = False
                clip_kwargs["audio_only"] = True
            try:
                session_info = self.molly.capture_av_clip(**clip_kwargs)
            except TypeError:
                clip_kwargs.pop("decode_video", None)
                clip_kwargs.pop("audio_only", None)
                session_info = self.molly.capture_av_clip(**clip_kwargs)
            if (session_info.get("audio_ok") or session_info.get("wav_saved")) and clip_wav.exists():
                audio_res = self.analyzer.analyze_dispense_audio(str(clip_wav))

        if audio_res:
            logger.info(
                "Audio Analysis -> motor=%s (%+.1f dB), kibble_peaks=%d (%+.1f dB), passed=%s | %s",
                audio_res.motor_detected, audio_res.motor_energy_db,
                audio_res.kibble_impact_peaks, audio_res.impact_energy_db,
                audio_res.kibble_dispensed_audio, audio_res.diagnosis,
            )

        visual_res: Optional[VisualAnalysisResult] = None
        if vision_enabled and after_jpg.exists():
            visual_res = self.analyzer.analyze_bowl_visual(
                after_image_path=str(after_jpg),
                before_image_path=str(before_jpg) if before_jpg.exists() else None,
                annotated_output_path=str(annotated_jpg),
            )
            ann_path = getattr(visual_res, "annotated_image_path", None)
            if ann_path and Path(ann_path).is_file():
                shutil.copyfile(ann_path, self.latest_snapshot)
            logger.info(
                "Visual Analysis -> before=%.1f%%, after=%.1f%%, delta=%+.1f%%, diff=%.1f%%, "
                "empty=%s, increased=%s | %s",
                visual_res.before_fill_ratio * 100, visual_res.after_fill_ratio * 100,
                visual_res.fill_delta_ratio * 100, visual_res.frame_diff_ratio * 100,
                visual_res.bowl_empty, visual_res.food_increased, visual_res.diagnosis,
            )
        return audio_res, visual_res, session_info

    def _capture_rtsp(
        self, req: VerifyRequest, before_jpg: Path, after_jpg: Path, clip_mp4: Path
    ) -> Dict[str, Any]:
        import subprocess

        def snap(dest: Path) -> bool:
            subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", self.cfg.camera_stream_url,
                 "-frames:v", "1", "-q:v", "2", str(dest)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
            )
            return dest.exists() and dest.stat().st_size > 0

        if req.expect_dispense:
            snap(before_jpg)
        if req.trigger_dispense:
            self.molly.dispense_food(req.portions)
        if req.expect_dispense:
            subprocess.run(
                ["ffmpeg", "-y", "-rtsp_transport", "tcp", "-i", self.cfg.camera_stream_url,
                 "-t", str(self.cfg.record_duration_seconds), "-c:v", "copy",
                 "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(clip_mp4)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.cfg.record_duration_seconds + 20, check=False,
            )
            time.sleep(2)
        snap(after_jpg)
        return {"rtsp_mode": True}

    def _decide(
        self,
        audio_res: Optional[AudioAnalysisResult],
        visual_res: Optional[VisualAnalysisResult],
        feed_abnormal: int,
        expect_dispense: bool,
    ) -> bool:
        return decide_passed(
            audio_res,
            visual_res,
            feed_abnormal,
            expect_dispense,
            vision_can_override_audio=bool(getattr(self.cfg, "vision_can_override_audio", False)),
        )

    def _check_feedback_override(
        self, passed: bool, expect_dispense: bool, audio_res: Optional[AudioAnalysisResult]
    ) -> Optional[str]:
        if passed or not expect_dispense or audio_res is None:
            return None
        return feedback_suggests_false_alarm(audio_res, self.db.feedback_labeled_events())

    @staticmethod
    def _build_summary(
        audio_res: Optional[AudioAnalysisResult],
        visual_res: Optional[VisualAnalysisResult],
        pre_weight: Any,
        post_weight: Any,
        feed_abnormal: int,
        fb_note: Optional[str],
        anomaly_note: str,
        session_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        parts = []
        if audio_res:
            parts.append(f"🔊 Sound: {audio_res.diagnosis}")
        if session_info is not None:
            a_sec = session_info.get("audio_sec")
            w_sec = session_info.get("recorded_wall_sec")
            last_rx = session_info.get("audio_last_rx_sec")
            gap_txt = (
                f"; audio dropped {w_sec - last_rx:.0f} s before the window ended"
                if isinstance(w_sec, (int, float)) and isinstance(last_rx, (int, float)) and (w_sec - last_rx) > 5.0
                else ""
            )
            parts.append(
                f"⚠️ Recording cut short: got {a_sec:.1f} s of audio out of {w_sec:.0f} s{gap_txt} "
                "(the amount dispensed couldn't be fully verified)."
            )
        if visual_res:
            parts.append(f"📷 Camera (secondary): {visual_res.diagnosis}")
            if visual_res.llm_reasoning:
                parts.append(f"🤖 LLM Vision: {visual_res.llm_reasoning}")
        if feed_abnormal > 1:
            parts.append(f"⚠️ Feeder hardware fault flag (DP 255 = {feed_abnormal})")
        if fb_note:
            parts.append(f"🧠 Feedback override: {fb_note}")
        if anomaly_note:
            parts.append(f"📉 Flow check: {anomaly_note}")
        return " | ".join(parts) if parts else "No verification telemetry captured"

    def _build_state(
        self,
        ts: str,
        req: VerifyRequest,
        transport: str,
        passed: bool,
        clog: bool,
        outcome: str,
        pre_weight: Any,
        post_weight: Any,
        feed_abnormal: int,
        audio_res: Optional[AudioAnalysisResult],
        visual_res: Optional[VisualAnalysisResult],
        session_info: Dict[str, Any],
        anomaly: Dict[str, Any],
        summary: str,
        before_jpg: Path,
        after_jpg: Path,
        annotated_jpg: Path,
        clip_mp4: Path,
        clip_wav: Path,
        cut_short: bool = False,
    ) -> Dict[str, Any]:
        return {
            "status": status_label(outcome, passed),
            "outcome": outcome,
            "verified": not is_unverified(outcome),
            "food_dispensed": True if passed else (None if is_unverified(outcome) else False),
            "recording_cut_short": cut_short,
            "last_check_time": ts,
            "mode": self.molly.mode,
            "active_transport": transport,
            "clog_detected": clog,
            "portions_requested": req.portions if req.expect_dispense else 0,
            "pre_weight_g": pre_weight,
            "post_weight_g": post_weight,
            "feed_abnormal": feed_abnormal,
            "audio": audio_res.__dict__ if audio_res else None,
            "visual": visual_res.__dict__ if visual_res else None,
            "capture_session": session_info,
            "anomaly": anomaly,
            "summary": summary,
            "before_image": str(before_jpg) if before_jpg.exists() else None,
            "after_image": str(after_jpg) if after_jpg.exists() else None,
            "annotated_image": str(annotated_jpg) if annotated_jpg.exists() else None,
            "video_clip": str(clip_mp4) if clip_mp4.exists() else None,
            "audio_clip": str(clip_wav) if clip_wav.exists() else None,
        }

    def _persist(
        self,
        state: Dict[str, Any],
        req: VerifyRequest,
        transport: str,
        passed: bool,
        clog: bool,
        outcome: str,
        audio_res: Optional[AudioAnalysisResult],
        visual_res: Optional[VisualAnalysisResult],
        pre_weight: Any,
        post_weight: Any,
        feed_abnormal: int,
        summary: str,
        before_jpg: Path,
        after_jpg: Path,
        annotated_jpg: Path,
        clip_mp4: Path,
        clip_wav: Path,
    ) -> int:
        audio_state = (
            "dispensed" if (audio_res and audio_res.kibble_dispensed_audio)
            else ("motor_only_clog" if (audio_res and audio_res.motor_detected)
                  else ("no_motor" if audio_res else "unavailable"))
        )
        return self.db.log_feed_event(
            event_type=req.label,
            mode=self.molly.mode,
            transport=transport,
            portions=req.portions if req.expect_dispense else 0,
            success=passed,
            clog_detected=clog,
            audio_state=audio_state,
            motor_detected=bool(audio_res and audio_res.motor_detected),
            kibble_peaks=int(audio_res.kibble_impact_peaks) if audio_res else 0,
            motor_energy_db=float(audio_res.motor_energy_db) if audio_res else 0.0,
            impact_energy_db=float(audio_res.impact_energy_db) if audio_res else 0.0,
            before_fill_ratio=float(visual_res.before_fill_ratio) if visual_res else 0.0,
            after_fill_ratio=float(visual_res.after_fill_ratio) if visual_res else 0.0,
            fill_delta_ratio=float(visual_res.fill_delta_ratio) if visual_res else 0.0,
            frame_diff_ratio=float(visual_res.frame_diff_ratio) if visual_res else 0.0,
            is_infrared=bool(visual_res and visual_res.is_infrared_mode),
            dog_at_bowl=bool(visual_res and visual_res.dog_at_bowl_detected),
            pre_weight_g=pre_weight,
            post_weight_g=post_weight,
            feed_abnormal=feed_abnormal,
            diagnosis=summary,
            before_image=str(before_jpg) if before_jpg.exists() else None,
            after_image=str(after_jpg) if after_jpg.exists() else None,
            annotated_image=str(annotated_jpg) if annotated_jpg.exists() else None,
            video_clip=str(clip_mp4) if clip_mp4.exists() else None,
            audio_clip=str(clip_wav) if clip_wav.exists() else None,
            raw_metrics=state,
            outcome=outcome,
            motor_duration_sec=float(audio_res.motor_duration_sec) if audio_res else 0.0,
            kibble_rate=float(audio_res.kibble_rate_per_sec) if audio_res else 0.0,
        )

    def _post_process(
        self,
        passed: bool,
        outcome: str,
        req: VerifyRequest,
        state: Dict[str, Any],
        summary: str,
        audio_res: Optional[AudioAnalysisResult],
        visual_res: Optional[VisualAnalysisResult],
        anomaly: Dict[str, Any],
        annotated_jpg: Path,
        clip_mp4: Path,
        clip_wav: Path,
        db_id: int,
    ) -> None:
        img = str(annotated_jpg) if annotated_jpg.exists() else None
        vid = str(clip_mp4) if clip_mp4.exists() else None
        aud = str(clip_wav) if clip_wav.exists() else None

        if passed:
            logger.info("FEED VERIFIED (db_id=#%d): %s", db_id, summary)
            if audio_res and audio_res.kibble_dispensed_audio:
                self.db.record_food_evidence("ting", f"event #{db_id}")
            elif visual_res and (visual_res.food_increased or visual_res.food_present_cv):
                self.db.record_food_evidence("camera", f"event #{db_id}")
            if state.get("recording_cut_short"):
                self.alerter.send_alert(
                    title="⚠️ Kibble Dispensed, Recording Cut Short",
                    message=(
                        f"Kibble was heard hitting the bowl, so the dogs were fed — "
                        f"but the camera stream dropped early and the full portion count couldn't be verified.\n\n"
                        f"{summary}"
                    ),
                    image_path=img, details=state, event="dispense_warning", urgent=False,
                )
            elif anomaly["short_run"] or anomaly["low_flow"] or anomaly["weak_flow"]:
                self.alerter.send_alert(
                    title="⚠️ Feeder Dispense Warning (Partial / Weak Flow)",
                    message=f"{anomaly['note']}\n\n{summary}",
                    image_path=img, details=state, event="dispense_warning", urgent=False,
                )
            else:
                self.alerter.notify_event("feed_verified", "✅ Meal Dispensed & Verified", summary, state)
            if (
                req.expect_dispense
                and self.schedule_eaten_check
                and self.cfg.eaten_check_minutes > 0
                and visual_res
                and not visual_res.is_infrared_mode
                and visual_res.after_fill_ratio >= self.cfg.min_bowl_fill_ratio
            ):
                self.schedule_eaten_check(visual_res.after_fill_ratio)
            return

        if not req.expect_dispense:
            self.alerter.send_alert(
                title="🥣 Dog Bowl Empty (On-Demand Check)",
                message=summary, image_path=img, details=state, event="bowl_empty_check", urgent=False,
            )
            return

        if outcome == CAMERA_UNAVAILABLE:
            disp_unv = bool((state.get("capture_session") or {}).get("dispensed_unverified"))
            title = (
                "📷 Feeder Camera Unreachable — Dispensed Unverified"
                if disp_unv else "📷 Feeder Camera Unreachable (Could Not Verify Meal)"
            )
            lead = (
                "Camera session failed twice, so the meal was dispensed WITHOUT audio/video verification."
                if disp_unv else "Could not open a camera/audio session to verify this scheduled meal."
            )
            self.alerter.send_alert(
                title=title, message=f"{lead}\n\n{summary}",
                details=state, event="camera_unavailable", urgent=False,
            )
            return

        if outcome == RECORDING_CUT_SHORT:
            cs = state.get("capture_session") or {}
            a_sec = cs.get("audio_sec")
            w_sec = cs.get("recorded_wall_sec")
            dur_note = (
                f"Only {a_sec:.1f} s of audio was captured out of the {w_sec:.0f} s window"
                if isinstance(a_sec, (int, float)) and isinstance(w_sec, (int, float))
                else "The camera/audio stream ended before the dispense window finished"
            )
            self.alerter.send_alert(
                title="📷 Recording Cut Short — Couldn't Verify Meal",
                message=(
                    f"{dur_note}, and no kibble was heard before the stream dropped. "
                    f"This is NOT flagged as a clog and will NOT trigger an auto-retry.\n\n"
                    f"• **Audio Check**: {audio_res.diagnosis if audio_res else 'Unavailable'}\n"
                    f"• **Visual Check**: {visual_res.diagnosis if visual_res else 'Unavailable'}"
                ),
                image_path=img, video_path=vid, audio_path=aud,
                details=state, event="recording_cut_short", urgent=False,
            )
            return

        if self.cfg.alert_mode == "no_food_hours":
            logger.warning("Feed problem (%s) logged only — ALERT_MODE=no_food_hours", outcome)
            return

        misses = self.db.consecutive_missed_feeds()
        escalate_at = max(1, getattr(self.cfg, "no_ting_escalate_after", 2))
        if outcome == MOTOR_NO_KIBBLE and misses < escalate_at:
            self.alerter.send_alert(
                title="⚠️ Meal Check: Motor Ran, No Kibble 'Ting' Heard",
                message=(
                    f"The auger motor ran for {audio_res.motor_duration_sec:.1f}s, "
                    f"but no kibble impacts were heard hitting the steel bowl "
                    f"(miss {misses} of {escalate_at} before urgent alarm).\n\n"
                    f"• **Audio Check**: {audio_res.diagnosis if audio_res else 'Unavailable'}\n"
                    f"• **Visual Check**: {visual_res.diagnosis if visual_res else 'Unavailable'}"
                ),
                image_path=img, video_path=vid, audio_path=aud,
                details=state, event="feed_no_ting_first", urgent=False,
            )
            return

        if outcome == MOTOR_NO_KIBBLE:
            title = (
                f"🚨 FEEDER CLOG / EMPTY HOPPER ({misses} Missed Meals in a Row)!"
                if misses >= 2 else "🚨 DOG FEEDER CLOG / EMPTY HOPPER!"
            )
            lead = "The feeder auger motor churned, but NO kibble was heard hitting the bowl."
        else:
            title = (
                f"🚨 DOG FEEDER DID NOT DISPENSE ({misses} Missed Meals in a Row)!"
                if misses >= 2 else "🚨 DOG FEEDER DID NOT DISPENSE!"
            )
            lead = "Scheduled meal time passed with NO auger motor sound and NO kibble heard."

        self.alerter.send_alert(
            title=title,
            message=(
                f"{lead}\n\n"
                f"• **Audio Check**: {audio_res.diagnosis if audio_res else 'Unavailable'}\n"
                f"• **Visual Check**: {visual_res.diagnosis if visual_res else 'Unavailable'}\n"
                f"• **Transport**: `{state['active_transport']}`"
            ),
            image_path=img, video_path=vid, audio_path=aud,
            details=state, event="feed_clog_detected", urgent=True,
        )

        tts_enabled = bool(getattr(self.cfg, "enable_tts", True)) and bool(self.cfg.tts_alert_on_clog)
        if tts_enabled and req.event_type != "scheduled_verify":
            self._broadcast_clog_tts()

    def _broadcast_clog_tts(self) -> None:
        if not bool(getattr(self.cfg, "enable_tts", True)) or not self.cfg.tts_alert_on_clog:
            return
        try:
            logger.warning("Broadcasting TTS clog alert over feeder speaker: '%s'", self.cfg.tts_alert_message)
            threading.Thread(
                target=self.molly.speak_tts,
                kwargs={
                    "text": self.cfg.tts_alert_message,
                    "voice": self.cfg.tts_voice,
                    "volume": self.cfg.tts_volume,
                    "repeat": self.cfg.tts_repeat,
                    "prepend_chime": True,
                },
                daemon=True,
            ).start()
        except Exception as exc:
            logger.error("Failed to launch speaker TTS alert: %s", exc)
