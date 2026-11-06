"""FeedVerifier: one verification run as a small pipeline (capture -> analyze -> decide -> persist -> notify)."""
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from watchdog.analysis import DEFAULT_KIBBLE_RATE, DEFAULT_MOTOR_SEC_PER_PORTION, assess_dispense
from watchdog.service import decision
from watchdog.service.models import CaptureFiles, CaptureResult, VerifyRequest

logger = logging.getLogger("poor_puppy.verifier")


class FeedVerifier:
    """Verifies that a meal actually reached the bowl, using audio (motor vs. kibble impacts),
    vision (before/after bowl) and feeder telemetry. Dependencies are injected explicitly."""

    def __init__(
        self,
        cfg,
        molly,
        analyzer,
        db,
        alerter,
        work_dir: Path,
        latest_snapshot_path: Path,
        on_meal_verified: Optional[Callable[[float], Any]] = None,
        after_event: Optional[Callable[[], Any]] = None,
        retention=None,
    ):
        self.cfg = cfg
        self.molly = molly
        self.analyzer = analyzer
        self.db = db
        self.alerter = alerter
        self.work_dir = work_dir
        self.latest_snapshot_path = latest_snapshot_path
        self.on_meal_verified = on_meal_verified  # called with the after-meal fill ratio (eaten check)
        self.after_event = after_event  # e.g. hopper-level check
        self.retention = retention  # RetentionManager (KEEP_FOOTAGE=false purges media after the alert)

    # ------------------------------------------------------------------ public API
    def run(self, req: VerifyRequest) -> Tuple[bool, Dict[str, Any]]:
        """Executes one verification. Returns (passed, state) where state feeds `/status`."""
        files = CaptureFiles.for_run(self.work_dir)
        logger.info(
            "Starting %s (mode=%s, trigger_dispense=%s, portions=%d)...",
            "feed verification" if req.expect_dispense else "on-demand bowl check",
            self.molly.mode,
            req.trigger_dispense,
            req.meal_portions,
        )
        cap = self._capture(req, files)
        audio_res, visual_res = self._analyze(cap, files)

        # Audio first: a recording the camera stream cut short can't prove a clog (only "unverified").
        cut_short = decision.recording_cut_short(
            cap.session,
            min_coverage=float(getattr(self.cfg, "recording_min_coverage", decision.DEFAULT_MIN_COVERAGE)),
            max_tail_gap_sec=float(getattr(self.cfg, "recording_max_tail_gap_sec", decision.DEFAULT_MAX_TAIL_GAP_SEC)),
        )
        passed = decision.decide_passed(
            audio_res, visual_res, cap.feed_abnormal, req.expect_dispense,
            vision_can_override_audio=bool(getattr(self.cfg, "vision_can_override_audio", False)),
        )
        outcome, passed = decision.classify(audio_res, visual_res, req.expect_dispense, passed, cut_short=cut_short)
        flags = self._assess(req, cap, audio_res, cut_short)

        details = self._details(req, cap, files, audio_res, visual_res, flags, outcome)
        summary = self._summary(cap, audio_res, visual_res, outcome=outcome, cut_short=cut_short)
        details["db_event_id"] = self._persist(req, cap, files, audio_res, visual_res, passed, outcome, summary, details)

        state = {
            "status": decision.status_label(outcome, passed),
            "mode": self.molly.mode,
            "last_check": datetime.now().isoformat(),
            "clog_detected": decision.is_clog(outcome, passed),
            "food_dispensed": passed,
            "verified": not decision.is_unverified(outcome),
            "outcome": outcome,
            "db_event_id": details["db_event_id"],
            "details": details,
        }
        if outcome == decision.DISPENSED:
            self.db.record_food_evidence("ting", f"event #{details['db_event_id']}")
        elif visual_res is not None and visual_res.food_present_cv:
            self.db.record_food_evidence("camera", f"event #{details['db_event_id']}")
        if self.after_event:
            self.after_event()
        if self.cfg.alert_mode == "no_food_hours" and not passed:
            # Timer mode: per-feed problems are only logged; the no-food timer decides when to alert.
            logger.warning("Feed problem (%s) logged only — ALERT_MODE=no_food_hours", outcome)
        else:
            self._notify(req, files, audio_res, visual_res, passed, outcome, flags, summary, details)
        if self.retention is not None:
            self.retention.purge_after_analysis(details["db_event_id"])  # no-op while KEEP_FOOTAGE=true
        return passed, state

    # ------------------------------------------------------------------ pipeline steps
    def _capture(self, req: VerifyRequest, files: CaptureFiles) -> CaptureResult:
        """Records the bowl (before / during / after) while optionally triggering the dispense."""
        pre_status = self.molly.get_status()
        dispense = {"ok": False, "transport": "none"}

        def _do_dispense():
            logger.info("Triggering live dispense of %d portion(s) via %s...", req.portions, self.molly.mode)
            dispense["ok"], dispense["transport"] = self.molly.dispense_food(req.portions)

        duration = float(req.record_seconds or self.cfg.record_duration_seconds)
        session: Dict[str, Any] = {}
        if self.cfg.camera_stream_url:
            # External RTSP/FLV stream
            url = self.cfg.camera_stream_url
            self.analyzer.capture_snapshot(url, files.before)
            if req.trigger_dispense:
                _do_dispense()
            _, audio_ok = self.analyzer.capture_av_clip(url, int(duration), files.video, files.audio)
            snap_ok = self.analyzer.capture_snapshot(url, files.after)
        else:
            # Native cloud camera session: before + dispense + clip + after in one session
            session = self.molly.capture_av_clip(
                duration_sec=duration,
                output_mp4=files.video,
                output_wav=files.audio,
                before_jpg=files.before,
                after_jpg=files.after,
                trigger_fn=_do_dispense if req.trigger_dispense else None,
                trigger_delay_sec=1.0,
            )
            audio_ok = bool(session.get("audio_ok"))
            snap_ok = bool(session.get("after_ok"))

        return CaptureResult(
            pre_status=pre_status,
            post_status=self.molly.get_status(),
            audio_ok=audio_ok,
            snapshot_ok=snap_ok,
            dispense_ok=dispense["ok"],
            dispense_transport=dispense["transport"],
            session=session,
        )

    def _analyze(self, cap: CaptureResult, files: CaptureFiles):
        audio_res = None
        if cap.audio_ok and Path(files.audio).exists():
            audio_res = self.analyzer.analyze_dispense_audio(files.audio)
            logger.info(
                "Audio Analysis -> motor=%s (+%.1f dB), kibble_peaks=%d (+%.1f dB), passed=%s | %s",
                audio_res.motor_detected, audio_res.motor_energy_db, audio_res.kibble_impact_peaks,
                audio_res.impact_energy_db, audio_res.kibble_dispensed_audio, audio_res.diagnosis,
            )
        visual_res = None
        if cap.snapshot_ok and Path(files.after).exists():
            visual_res = self.analyzer.analyze_bowl_visual(
                after_image_path=files.after,
                before_image_path=files.existing("before"),
                annotated_output_path=files.annotated,
            )
            if Path(files.annotated).exists():
                shutil.copyfile(files.annotated, self.latest_snapshot_path)
            logger.info(
                "Visual Analysis -> before=%.1f%%, after=%.1f%%, delta=%+.1f%%, diff=%.1f%%, empty=%s, increased=%s | %s",
                visual_res.before_fill_ratio * 100, visual_res.after_fill_ratio * 100,
                visual_res.fill_delta_ratio * 100, visual_res.frame_diff_ratio * 100,
                visual_res.bowl_empty, visual_res.food_increased, visual_res.diagnosis,
            )
        return audio_res, visual_res

    def _assess(self, req: VerifyRequest, cap: CaptureResult, audio_res, cut_short: bool = False) -> Dict[str, Any]:
        """Early-warning flags (short run / low flow) against this feeder's learned baselines.
        Never claimed on a recording the camera stream cut short (the motor run / flow wasn't fully heard)."""
        learned = self.db.learned_baselines()
        max_ext = float(os.getenv("RECORD_MAX_EXTEND_SECONDS", "60"))
        flags = assess_dispense(
            audio_res,
            req.meal_portions,
            motor_sec_per_portion=learned.get("motor_sec_per_portion") or DEFAULT_MOTOR_SEC_PER_PORTION,
            baseline_rate=learned.get("kibble_rate") or DEFAULT_KIBBLE_RATE,
            recording_truncated=cut_short or float(cap.session.get("recording_extended_sec") or 0) >= max_ext - 1,
            weak_flow_ting_rate=float(getattr(self.cfg, "weak_flow_ting_rate", 4.6)),
        )
        if cut_short:
            flags["short_run"] = False
            flags["low_flow"] = False
            flags["weak_flow"] = False
        flags["recording_cut_short"] = bool(cut_short)
        return flags

    def _details(self, req, cap, files, audio_res, visual_res, flags, outcome) -> Dict[str, Any]:
        transport = cap.dispense_transport if req.trigger_dispense else str(cap.post_status.get("active_transport") or "cloud")
        return {
            "mode": self.molly.mode,
            "transport": transport,
            "trigger_dispense": req.trigger_dispense,
            "dispense_ok": cap.dispense_ok if req.trigger_dispense else None,
            "portions_requested": req.meal_portions,
            "warnings": flags,
            "motor_window_sec": cap.session.get("motor_window_sec"),
            "recording_extended_sec": cap.session.get("recording_extended_sec"),
            # Recording coverage (None = unknown for this capture path)
            "recorded_wall_sec": cap.session.get("recorded_wall_sec"),
            "audio_sec": cap.session.get("audio_sec"),
            "audio_last_rx_sec": cap.session.get("audio_last_rx_sec"),
            "video_last_rx_sec": cap.session.get("video_last_rx_sec"),
            "recording_cut_short": bool(flags.get("recording_cut_short")),
            "pre_food_weight_g": cap.pre_status.get("food_weight_g"),
            "post_food_weight_g": cap.post_status.get("food_weight_g"),
            "pre_history": cap.pre_status.get("dps", {}).get("206"),
            "post_history": cap.post_status.get("dps", {}).get("206"),
            "feed_abnormal": cap.feed_abnormal,
            "audio": audio_res.__dict__ if audio_res else None,
            "visual": visual_res.__dict__ if visual_res else None,
            "before_image": files.existing("before"),
            "after_image": files.existing("after"),
            "annotated_image": files.existing("annotated"),
            "video_clip": files.existing("video"),
            "audio_clip": files.existing("audio"),
            "outcome": outcome,
        }

    def _summary(self, cap: CaptureResult, audio_res, visual_res, outcome: Optional[str] = None,
                 cut_short: bool = False) -> str:
        """Plain-text summary (no markdown: Home Assistant shows `**` literally). Sound first — it is the
        reliable signal — then the camera, marked secondary."""
        lines = []
        if audio_res is not None:
            if outcome == decision.RECORDING_CUT_SHORT:
                heard = (f"motor heard for {audio_res.motor_duration_sec:.1f}s, but no kibble heard"
                         if audio_res.motor_detected else "no motor or kibble heard")
                lines.append(f"🔊 Sound: {heard} in the part that was recorded.")
            else:
                lines.append(f"🔊 Sound: {audio_res.diagnosis}")
        elif visual_res is not None:
            lines.append("🔊 Sound: no audio was captured, so only the camera could check.")
        if cut_short:
            got, wall = cap.session.get("audio_sec"), cap.session.get("recorded_wall_sec")
            line = f"⚠️ Recording cut short: got {float(got or 0):.1f} s of audio out of {float(wall or 0):.0f} s"
            if outcome == decision.DISPENSED:
                line += " (the amount dispensed couldn't be fully verified)"
            lines.append(line + ".")
        if visual_res is not None:
            lines.append(f"📷 Camera (secondary): {visual_res.diagnosis}")
            if visual_res.llm_reasoning:
                lines.append(f"📷 Vision AI (secondary): {visual_res.llm_reasoning}")
        if cap.feed_abnormal > 1:
            # DP 255 reads 1 on every feed (as does the DP 202 "weight"): only a higher code is a real fault
            lines.append(f"⚠️ Feeder reported fault code {cap.feed_abnormal}")
        if not lines:
            lines.append("Nothing was captured: no audio and no bowl image (camera unreachable).")
        return "\n".join(lines)

    def _persist(self, req, cap, files, audio_res, visual_res, passed, outcome, summary, details) -> int:
        a, v = audio_res, visual_res
        return self.db.log_feed_event(
            event_type=req.db_event_type,
            mode=self.molly.mode,
            transport=details["transport"],
            portions=req.meal_portions,
            success=passed,
            clog_detected=decision.is_clog(outcome, passed),
            audio_state=decision.audio_state(a),
            motor_detected=a.motor_detected if a else False,
            kibble_peaks=a.kibble_impact_peaks if a else 0,
            motor_db=a.motor_energy_db if a else 0.0,
            impact_db=a.impact_energy_db if a else 0.0,
            before_fill_pct=(v.before_fill_ratio * 100.0) if v else 0.0,
            after_fill_pct=(v.after_fill_ratio * 100.0) if v else 0.0,
            fill_delta_pct=(v.fill_delta_ratio * 100.0) if v else 0.0,
            bowl_empty=v.bowl_empty if v else False,
            food_increased=v.food_increased if v else False,
            dog_in_bowl=v.dog_at_bowl_detected if v else False,
            infrared_mode=v.is_infrared_mode if v else False,
            food_weight_g=cap.post_status.get("food_weight_g"),
            feed_abnormal=cap.feed_abnormal,
            before_image=details["before_image"],
            after_image=details["after_image"],
            annotated_image=details["annotated_image"],
            video_clip=details["video_clip"],
            audio_clip=details["audio_clip"],
            webhook_sent=True,
            diagnosis=summary.replace("\n", " | "),
            details=details,
            outcome=outcome,
            motor_duration_sec=a.motor_duration_sec if a else None,
            kibble_rate=a.kibble_rate_per_sec if a else None,
        )

    # ------------------------------------------------------------------ notifications
    def _notify(self, req, files, audio_res, visual_res, passed, outcome, flags, summary, details) -> None:
        image = files.existing("annotated")
        if passed:
            self._warn_if_off(req, audio_res, flags, image, details)
            self._schedule_eaten_check(req, visual_res)
            label = "BOWL CHECK OK" if outcome == decision.BOWL_CHECK else "FEED VERIFIED"
            logger.info("%s (db_id=#%d): %s", label, details["db_event_id"], summary.replace("\n", " | "))
            self.alerter.send_ha_webhook(
                event="feed_verified", clog_detected=False, title="Puppy Feed Verified", message=summary, details=details
            )
            return
        if outcome == decision.BOWL_CHECK:
            self.alerter.send_alert(title="Bowl looks empty", message=summary, image_path=image,
                                    event="bowl_empty_check", details=details, urgent=False)
            return
        if outcome == decision.CAMERA_UNAVAILABLE:
            self.alerter.send_alert(
                title="Couldn't verify the meal: feeder camera unreachable",
                message=(("The dispense command was sent, but " if req.trigger_dispense else "")
                         + "no video/audio could be captured, so the feed is UNVERIFIED. "
                         "No automatic retry was done (to avoid overfeeding)."),
                event="camera_unavailable", details=details, urgent=False,
            )
            return
        if outcome == decision.RECORDING_CUT_SHORT:
            # Capture failure, not a clog: no escalation counter, no TTS, no auto-retry (never dispense blind)
            self.alerter.send_alert(
                title="Couldn't verify the meal: camera stream dropped",
                message=self._cut_short_message(req, audio_res, details) + "\n" + summary,
                image_path=image, event="recording_cut_short", details=details, urgent=False,
            )
            return
        # Escalation (owner's rule): one feed without the kibble "ting" can be a fluke (dog's head in the bowl,
        # noise), so it's a heads-up. N in a row => clog / no food. Food still visible => dog is OK for now.
        misses = self.db.consecutive_missed_feeds()
        food_seen = bool(visual_res is not None and visual_res.food_present_cv)
        details["consecutive_missed_feeds"] = misses
        details["food_seen_in_bowl"] = food_seen
        if misses < self.cfg.no_ting_escalate_after:
            self.alerter.send_alert(
                title=f"No kibble sound on this feed ({misses}/{self.cfg.no_ting_escalate_after}) — watching the next one",
                message=summary + ("\n• Bowl still has food: dog is OK for now." if food_seen else ""),
                image_path=image, event="feed_no_ting_first", details=details, urgent=False,
            )
            return
        title = decision.clog_alert_title(outcome) + f" ({misses} feeds in a row)"
        if food_seen:
            self.alerter.send_alert(title=title + " — bowl still has some food, dog OK for now",
                                    message=summary, image_path=image, event="feed_clog_detected",
                                    details=details, urgent=False)
            return
        self.alerter.send_alert(title=title, message=summary, image_path=image,
                                event="feed_clog_detected", details=details)
        if req.announce_clog_tts:
            self._announce_clog(details)

    @staticmethod
    def _cut_short_message(req, audio_res, details: Dict[str, Any]) -> str:
        got = float(details.get("audio_sec") or 0)
        wall = float(details.get("recorded_wall_sec") or 0)
        what = "any kibble" if (audio_res is not None and audio_res.motor_detected) else "the motor"
        msg = (f"The camera stream delivered only {got:.1f} s of audio out of the {wall:.0f} s recording, "
               f"before {what} could be heard. ")
        vid = details.get("video_last_rx_sec")
        if vid is not None and float(vid) > got + 5:
            msg += (f"The video kept recording until {float(vid):.0f} s (only the audio dropped out): "
                    "check the video / AFTER photo for kibble in the bowl. ")
        if req.trigger_dispense:
            msg += ("The dispense command WAS sent, so food was probably dispensed: check the bowl. "
                    if details.get("dispense_ok") else
                    "The dispense command could not be confirmed as sent: check the bowl. ")
        return msg + "The feed is UNVERIFIED (not a clog). No automatic retry was done (to avoid overfeeding)."

    def _warn_if_off(self, req, audio_res, flags, image, details) -> None:
        why = []
        if flags.get("short_run"):
            why.append(
                f"motor ran {audio_res.motor_duration_sec:.0f}s but ~{flags['expected_motor_sec']:.0f}s was expected "
                f"for {req.meal_portions} portions (stopped early: jam / partial dispense?)"
            )
        if flags.get("low_flow"):
            why.append(
                f"kibble flow {audio_res.kibble_rate_per_sec:.1f} impacts/s is far below normal "
                "(partial clog, hopper running low, or a very full bowl)"
            )
        if flags.get("weak_flow") and not flags.get("low_flow"):
            why.append(
                f"kibble flow was weaker than normal ({audio_res.ting_rate_per_sec:.1f} kibble hits/s; normal is "
                f"{getattr(self.cfg, 'weak_flow_ting_rate', 4.6):.1f}+): possible partial jam or the hopper running low"
            )
        if why:
            self.alerter.send_alert(title="Feed went through, but something looks off",
                                    message="\n".join(f"• {w}" for w in why), image_path=image,
                                    event="dispense_warning", details=details, urgent=False)

    def _schedule_eaten_check(self, req, visual_res) -> None:
        if (
            self.on_meal_verified
            and req.expect_dispense
            and self.cfg.eaten_check_minutes > 0
            and visual_res is not None
            and not visual_res.is_infrared_mode
            and visual_res.after_fill_ratio >= 0.05
        ):
            t = threading.Timer(self.cfg.eaten_check_minutes * 60.0, self.on_meal_verified,
                                args=(visual_res.after_fill_ratio,))
            t.daemon = True
            t.start()

    def _announce_clog(self, details: Dict[str, Any]) -> None:
        """Voice alert on the feeder's own speaker."""
        if not (self.cfg.tts_alert_on_clog and self.cfg.tts_alert_message):
            return
        logger.warning("Broadcasting TTS clog alert over feeder speaker: %r", self.cfg.tts_alert_message)
        res = self.molly.speak_tts(text=self.cfg.tts_alert_message, voice=self.cfg.tts_voice,
                                   volume=self.cfg.tts_volume, repeat=self.cfg.tts_repeat, prepend_chime=True)
        details["tts_alert"] = res
        self.db.log_action(action="tts_clog_alert", transport="cloud_imm", success=bool(res.get("ok")),
                           summary=f"Spoke clog alert on feeder speaker: {self.cfg.tts_alert_message}", payload=res)
