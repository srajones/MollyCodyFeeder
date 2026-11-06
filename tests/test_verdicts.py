"""Verdict rules (v0.6): audio-first decisions, recording-cut-short handling, honest notifications.

Run:  .venv/bin/python -m unittest discover -s tests -v

Pure / fake-collaborator tests only: no network, no camera, no cloud login.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WEBHOOK_SECRET", "")

from watchdog.analysis.results import AudioAnalysisResult, VisualAnalysisResult  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402
from watchdog.service import decision as d  # noqa: E402


def audio(kibble=False, motor=True, motor_sec=3.0, diagnosis=None):
    diag = diagnosis or (
        "Motor churned AND kibble heard hitting the bowl." if kibble
        else ("CLOG / EMPTY HOPPER SOUND DETECTED: Motor churned without the kibble 'ting'." if motor
              else "No sustained auger motor hum or kibble impacts detected on audio.")
    )
    return AudioAnalysisResult(motor, 40 if kibble else 5, kibble, 30.0, 10.0, diag, motor_sec if motor else 0.0)


def visual(**kw):
    base = dict(before_fill_ratio=0.0, after_fill_ratio=0.0, fill_delta_ratio=0.0, frame_diff_ratio=0.0,
                bowl_empty=False, food_increased=False, food_present_cv=False, food_present_llm=None,
                llm_reasoning="", annotated_image_path="", diagnosis="IR night vision: can't judge the bowl at night; sound decides.")
    base.update(kw)
    return VisualAnalysisResult(**base)


# Real event #1 (manual feed + verify): 1.36 s of audio arrived in a 13 s recording
CUT_SESSION = {"recorded_wall_sec": 13.0, "audio_sec": 1.36, "audio_last_rx_sec": 1.4}
FULL_SESSION = {"recorded_wall_sec": 13.0, "audio_sec": 13.0, "audio_last_rx_sec": 12.96}


class TestRecordingCutShort(unittest.TestCase):
    def test_unknown_or_short_windows_are_not_cut_short(self):
        self.assertFalse(d.recording_cut_short(None))
        self.assertFalse(d.recording_cut_short({}))
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 13.0}))  # audio_sec unknown
        self.assertFalse(d.recording_cut_short({"audio_sec": 1.0}))  # wall unknown
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": "n/a", "audio_sec": 1.0}))
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 2.5, "audio_sec": 0.1, "audio_last_rx_sec": 0.1}))

    def test_coverage_and_tail_gap(self):
        self.assertTrue(d.recording_cut_short(CUT_SESSION))
        self.assertFalse(d.recording_cut_short(FULL_SESSION))
        # Real event #25: audio kept trickling in until 92 s, but only 26 s of it arrived in a 95 s window
        self.assertTrue(d.recording_cut_short({"recorded_wall_sec": 95.0, "audio_sec": 26.5, "audio_last_rx_sec": 92.5}))
        # Full coverage but the stream went silent 8 s before the end
        self.assertTrue(d.recording_cut_short({"recorded_wall_sec": 80.0, "audio_sec": 72.0, "audio_last_rx_sec": 72.0}))
        # Normal packet loss (#18: 72.7 s of 85 s) is fine
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 85.0, "audio_sec": 72.7, "audio_last_rx_sec": 84.9}))
        # Missing last-rx time: only the coverage rule applies
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 80.0, "audio_sec": 79.0}))

    def test_thresholds_are_configurable(self):
        s = {"recorded_wall_sec": 100.0, "audio_sec": 70.0, "audio_last_rx_sec": 97.0}
        self.assertTrue(d.recording_cut_short(s))
        self.assertFalse(d.recording_cut_short(s, min_coverage=0.5))
        self.assertTrue(d.recording_cut_short(s, min_coverage=0.5, max_tail_gap_sec=2.0))
        with mock.patch.dict(os.environ, {"RECORDING_MIN_COVERAGE": "0.5", "RECORDING_MAX_TAIL_GAP_SEC": "9",
                                          "VISION_CAN_OVERRIDE_AUDIO": "true"}):
            import importlib

            import watchdog.config as wc
            cfg = importlib.reload(wc).WatchdogConfig()
            self.assertEqual((cfg.recording_min_coverage, cfg.recording_max_tail_gap_sec), (0.5, 9.0))
            self.assertTrue(cfg.vision_can_override_audio)
        cfg = importlib.reload(wc).WatchdogConfig()
        self.assertEqual((cfg.recording_min_coverage, cfg.recording_max_tail_gap_sec), (0.75, 5.0))
        self.assertFalse(cfg.vision_can_override_audio)

    def test_camera_coverage_helper(self):
        import numpy as np

        from watchdog.molly.camera import _recording_coverage

        chunks = [np.zeros(320, np.int16)] * 34  # 34 x 40 ms = 1.36 s
        cov = _recording_coverage(chunks, 8000, [100.5, 101.4], 100.0, 113.0)
        self.assertEqual(cov, {"recorded_wall_sec": 13.0, "audio_sec": 1.36, "audio_last_rx_sec": 1.4})
        self.assertTrue(d.recording_cut_short(cov))
        self.assertIsNone(_recording_coverage([], 8000, [], 0.0, 13.0)["audio_last_rx_sec"])


class TestAudioFirstDecision(unittest.TestCase):
    def _run(self, a, v, cut, expect=True, override=False, abnormal=0):
        passed = d.decide_passed(a, v, abnormal, expect, vision_can_override_audio=override)
        return d.classify(a, v, expect, passed, cut_short=cut)

    def test_kibble_heard_passes_even_when_cut_short(self):
        self.assertEqual(self._run(audio(kibble=True), visual(), cut=True), (d.DISPENSED, True))
        self.assertEqual(self._run(audio(kibble=True), None, cut=False), (d.DISPENSED, True))

    def test_vision_cannot_flip_a_complete_audio_verdict(self):
        # Real event #25 shape: motor ran, no kibble heard, IR "bowl got darker" said food increased
        ir_false_positive = visual(food_increased=True, is_infrared_mode=True)
        self.assertEqual(self._run(audio(kibble=False), ir_false_positive, cut=False), (d.MOTOR_NO_KIBBLE, False))
        self.assertEqual(self._run(audio(motor=False), visual(food_increased=True), cut=False), (d.NO_MOTOR, False))
        self.assertEqual(self._run(audio(kibble=False), visual(food_present_llm=True), cut=False), (d.MOTOR_NO_KIBBLE, False))
        # Legacy behaviour behind VISION_CAN_OVERRIDE_AUDIO=true
        self.assertTrue(self._run(audio(kibble=False), ir_false_positive, cut=False, override=True)[1])

    def test_cut_short_without_kibble_is_unverified_not_clog(self):
        for a in (audio(motor=False), audio(kibble=False)):
            outcome, passed = self._run(a, visual(food_increased=True, is_infrared_mode=True), cut=True)
            self.assertEqual((outcome, passed), (d.RECORDING_CUT_SHORT, False))
            self.assertFalse(d.is_clog(outcome, passed))
            self.assertNotIn(outcome, d.CLOG_OUTCOMES)
            self.assertEqual(d.status_label(outcome, passed), "unverified")
            self.assertTrue(d.is_unverified(outcome))
        self.assertEqual(d.clog_alert_title(d.MOTOR_NO_KIBBLE).split(":")[0], "FEEDER CLOG OR EMPTY HOPPER")

    def test_no_audio_keeps_vision_fallback(self):
        self.assertEqual(self._run(None, visual(food_increased=True), cut=True), (d.UNVERIFIED, True))
        self.assertEqual(self._run(None, visual(), cut=True), (d.UNVERIFIED, False))
        self.assertEqual(self._run(None, None, cut=True), (d.CAMERA_UNAVAILABLE, False))
        # On-demand bowl checks are unaffected by the cut-short rule
        self.assertEqual(self._run(audio(motor=False), visual(food_present_cv=True), cut=True, expect=False),
                         (d.BOWL_CHECK, True))

    def test_hardware_fault_still_fails(self):
        self.assertEqual(self._run(audio(kibble=True), None, cut=False, abnormal=2), (d.DISPENSED, False))


class TestVisionDiagnosis(unittest.TestCase):
    def _diag(self, **kw):
        from watchdog.analysis.vision import VisionMixin

        base = dict(is_ir=False, has_before=True, dog_at_bowl=False, bowl_empty=False, food_increased=False,
                    food_present_cv=False, before_fill=0.0, after_fill=0.0, fill_delta=0.0)
        base.update(kw)
        return VisionMixin._visual_diagnosis(**base)

    def test_ir_never_prints_fill_percentages(self):
        txt = self._diag(is_ir=True, food_increased=True, ir_darker=0.394, after_fill=0.0)
        self.assertEqual(txt, "IR night vision: new kibble seen (bowl floor darker in 39% of the area).")
        self.assertIn("reflection 45% covered", self._diag(is_ir=True, food_increased=True, ir_darker=0.01, ir_occlusion=0.45))
        for kw in ({}, {"dog_at_bowl": True}, {"has_before": False}):
            txt = self._diag(is_ir=True, **kw)
            self.assertIn("sound decides", txt)
            self.assertNotIn("fill", txt)
            self.assertNotIn("0.0%", txt)

    def test_daytime_wording_is_not_contradictory(self):
        self.assertIn("1.0% -> 6.0%", self._diag(food_increased=True, food_present_cv=True, before_fill=0.01, after_fill=0.06))
        # Old text claimed "Bowl already had food (0.0%)" when nothing was measured
        txt = self._diag(after_fill=0.0)
        self.assertNotIn("already had food", txt)
        self.assertIn("can't tell", txt)
        self.assertIn("Food in the bowl", self._diag(food_present_cv=True, after_fill=0.08))
        self.assertIn("sound decides", self._diag(dog_at_bowl=True))
        self.assertIn("Camera can't tell", self._diag(has_before=False))


def _verifier(td, audio_res, visual_res, session, abnormal=0, misses=2):
    from watchdog.service.verifier import FeedVerifier

    cfg = WatchdogConfig()
    cfg.camera_stream_url = ""
    cfg.eaten_check_minutes = 0
    cfg.tts_alert_on_clog = True
    cfg.alert_mode = "every_miss"
    cfg.vision_can_override_audio = False
    molly = mock.Mock(mode="cloud")
    molly.get_status.return_value = {"food_weight_g": 10, "feed_abnormal": abnormal, "dps": {}}
    molly.dispense_food.return_value = (True, "cloud")

    def capture(**kw):
        if kw.get("trigger_fn"):
            kw["trigger_fn"]()
        for k in ("output_wav", "before_jpg", "after_jpg"):
            Path(kw[k]).write_bytes(b"x")
        return {"audio_ok": True, "after_ok": True, "motor_window_sec": None, "recording_extended_sec": 0, **session}

    molly.capture_av_clip.side_effect = capture
    molly.speak_tts.return_value = {"ok": True}
    analyzer = mock.Mock()
    analyzer.analyze_dispense_audio.return_value = audio_res
    analyzer.analyze_bowl_visual.return_value = visual_res
    db = mock.Mock()
    db.learned_baselines.return_value = {}
    db.log_feed_event.return_value = 7
    db.consecutive_missed_feeds.return_value = misses
    v = FeedVerifier(cfg, molly, analyzer, db, mock.Mock(), Path(td), Path(td) / "latest.jpg")
    return v, molly, db


class TestVerifierVerdicts(unittest.TestCase):
    def test_cut_short_manual_feed_is_unverified_alert(self):
        from watchdog.service.models import VerifyRequest

        with tempfile.TemporaryDirectory() as td:
            v, molly, db = _verifier(td, audio(motor=False), visual(food_increased=True, is_infrared_mode=True), CUT_SESSION)
            passed, state = v.run(VerifyRequest(trigger_dispense=True, portions=1, record_seconds=12))
            self.assertFalse(passed)
            self.assertEqual((state["outcome"], state["status"]), ("recording_cut_short", "unverified"))
            self.assertFalse(state["clog_detected"])
            self.assertFalse(state["verified"])
            kw = db.log_feed_event.call_args.kwargs
            self.assertFalse(kw["clog_detected"])
            self.assertFalse(kw["success"])
            self.assertEqual(kw["outcome"], "recording_cut_short")
            self.assertTrue(kw["details"]["recording_cut_short"])
            self.assertEqual(kw["details"]["audio_sec"], 1.36)
            db.consecutive_missed_feeds.assert_not_called()  # never part of the clog escalation
            v.alerter.send_alert.assert_called_once()
            a = v.alerter.send_alert.call_args.kwargs
            self.assertEqual(a["event"], "recording_cut_short")
            self.assertEqual(a["title"], "Couldn't verify the meal: camera stream dropped")
            self.assertFalse(a["urgent"])
            self.assertIn("only 1.4 s of audio out of the 13 s recording, before the motor could be heard", a["message"])
            self.assertIn("dispense command WAS sent", a["message"])
            self.assertIn("UNVERIFIED (not a clog)", a["message"])
            self.assertIn("No automatic retry", a["message"])
            self.assertNotIn("CLOG / EMPTY HOPPER", a["message"])
            self.assertNotIn("**", a["message"])
            molly.speak_tts.assert_not_called()  # no TTS clog announcement
            molly.dispense_food.assert_called_once_with(1)

    def test_cut_short_but_kibble_heard_passes_with_warning(self):
        from watchdog.service.models import VerifyRequest

        session = {"recorded_wall_sec": 83.0, "audio_sec": 46.1, "audio_last_rx_sec": 81.5}  # real event #8
        with tempfile.TemporaryDirectory() as td:
            v, molly, db = _verifier(td, audio(kibble=True, motor_sec=26.3), visual(), session)
            passed, state = v.run(VerifyRequest(expected_portions=17, expect_dispense=True))
            self.assertTrue(passed)
            self.assertEqual(state["outcome"], "dispensed")
            details = db.log_feed_event.call_args.kwargs["details"]
            self.assertTrue(details["warnings"]["recording_cut_short"])
            self.assertFalse(details["warnings"]["short_run"])  # 26 s of 40 s expected, but the stream dropped
            self.assertFalse(details["warnings"]["low_flow"])
            v.alerter.send_alert.assert_not_called()  # no "stopped early" warning claimed
            msg = v.alerter.send_ha_webhook.call_args.kwargs["message"]
            self.assertIn("⚠️ Recording cut short: got 46.1 s of audio out of 83 s", msg)
            self.assertIn("couldn't be fully verified", msg)

    def test_event25_shape_complete_recording_is_not_flipped_by_vision(self):
        from watchdog.service.models import VerifyRequest

        with tempfile.TemporaryDirectory() as td:
            v, molly, db = _verifier(td, audio(kibble=False, motor_sec=11.2),
                                     visual(food_increased=True, is_infrared_mode=True), FULL_SESSION, misses=1)
            passed, state = v.run(VerifyRequest(expected_portions=20, expect_dispense=True))
            self.assertFalse(passed)
            self.assertEqual(state["outcome"], "motor_no_kibble")
            kw = db.log_feed_event.call_args.kwargs
            self.assertEqual((kw["success"], kw["clog_detected"]), (False, True))  # no contradictory success=1
            self.assertEqual(v.alerter.send_alert.call_args.kwargs["event"], "feed_no_ting_first")

    def test_summary_is_plain_audio_first_without_noise_telemetry(self):
        from watchdog.service.models import CaptureResult

        with tempfile.TemporaryDirectory() as td:
            v, _, _ = _verifier(td, None, None, {})
            cap = CaptureResult(pre_status={}, post_status={"food_weight_g": 10, "feed_abnormal": 1},
                                audio_ok=True, snapshot_ok=True, session=dict(FULL_SESSION))
            vis = visual(diagnosis="IR night vision: new kibble seen (bowl floor darker in 39% of the area).",
                         llm_reasoning="Kibble visible.")
            s = v._summary(cap, audio(kibble=True), vis, outcome=d.DISPENSED)
            lines = s.split("\n")
            self.assertTrue(lines[0].startswith("🔊 Sound: "))
            self.assertTrue(lines[1].startswith("📷 Camera (secondary): IR night vision"))
            self.assertTrue(lines[2].startswith("📷 Vision AI (secondary): "))
            for bad in ("**", "weight=", "abnormal=", "Telemetry", "before=", "0.0%"):
                self.assertNotIn(bad, s)
            cap.post_status["feed_abnormal"] = 3
            self.assertIn("⚠️ Feeder reported fault code 3", v._summary(cap, audio(kibble=True), None, outcome=d.DISPENSED))
            # No audio: the summary says so instead of silently relying on vision
            self.assertIn("no audio was captured", v._summary(cap, None, vis, outcome=d.UNVERIFIED))
            # Nothing captured: still a readable DB diagnosis
            cap.post_status["feed_abnormal"] = 1
            self.assertIn("Nothing was captured", v._summary(cap, None, None, outcome=d.CAMERA_UNAVAILABLE))


class TestHaPayloadHonesty(unittest.TestCase):
    def _alerter(self, notify_on_success=False):
        from watchdog.alerter import HouseholdAlerter

        cfg = WatchdogConfig()
        cfg.ha_webhook_url = "https://ha.example/api/webhook/hook"
        cfg.ha_notify_on_success = notify_on_success
        cfg.discord_webhook_url = cfg.telegram_bot_token = cfg.ntfy_topic_url = ""
        cfg.alert_cooldown_minutes = 0
        return HouseholdAlerter(cfg)

    def _payload_for(self, event, details=None, **kw):
        a = self._alerter()
        with mock.patch("watchdog.alerter.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            a.send_alert("Title **bold**", "• **Audio Check**: x", event=event, details=details, **kw)
            post.assert_called_once()
            return post.call_args.kwargs["json"]

    def test_only_real_clog_events_are_clogs(self):
        for ev in ("feed_clog_detected", "feed_no_ting_first", "hopper_empty_suspected", "retry_budget_exhausted",
                   "no_food_timer"):
            p = self._payload_for(ev)
            self.assertEqual((p["clog_detected"], p["food_dispensed"], p["verified"]), (True, False, True), ev)
        for ev in ("camera_unavailable", "recording_cut_short", "schedule_changed", "schedule_empty", "feeder_offline",
                   "hopper_low", "food_not_eaten", "daily_summary", "bowl_empty_check"):
            p = self._payload_for(ev)
            self.assertEqual((p["clog_detected"], p["food_dispensed"], p["verified"]), (False, None, False), ev)
        for ev in ("retry_succeeded", "dispense_warning"):
            p = self._payload_for(ev)
            self.assertEqual((p["clog_detected"], p["food_dispensed"], p["verified"]), (False, True, True), ev)

    def test_payload_is_plain_text_and_keeps_legacy_keys(self):
        p = self._payload_for("recording_cut_short", details={"outcome": "recording_cut_short", "clog_detected": True})
        for key in ("event", "event_type", "clog_detected", "food_dispensed", "title", "message", "timestamp",
                    "snapshot_url", "verified", "outcome"):
            self.assertIn(key, p)
        self.assertEqual(p["outcome"], "recording_cut_short")
        self.assertFalse(p["clog_detected"])  # a stale flag inside details can't override the verdict
        self.assertNotIn("**", p["title"] + p["message"])
        self.assertEqual(p["title"], "Title bold")

    def test_feed_verified_webhook_and_success_gating(self):
        a = self._alerter(notify_on_success=True)
        with mock.patch("watchdog.alerter.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            self.assertTrue(a.send_ha_webhook("feed_verified", False, "T", "🔊 Sound: ok", {"outcome": "dispensed"}))
            p = post.call_args.kwargs["json"]
            self.assertEqual((p["food_dispensed"], p["verified"], p["outcome"]), (True, True, "dispensed"))
        a = self._alerter(notify_on_success=False)
        with mock.patch("watchdog.alerter.requests.post") as post:
            post.return_value = mock.Mock(status_code=200)
            self.assertFalse(a.send_ha_webhook("feed_verified", False, "T", "M", {}))
            post.assert_not_called()
            # Informational alerts are no longer flagged as clogs, but they still reach HA
            self.assertTrue(a.send_alert("Couldn't verify", "m", event="camera_unavailable", urgent=False))
            post.assert_called_once()
            self.assertFalse(post.call_args.kwargs["json"]["clog_detected"])


class TestNoAutoRetryOnCutShort(unittest.TestCase):
    def _watchdog(self, outcomes):
        from watchdog.service.scheduling import SchedulingMixin

        class W(SchedulingMixin):
            pass

        w = W()
        w.cfg = WatchdogConfig()
        w.cfg.auto_retry_on_clog = True
        w.cfg.max_retry_attempts = 2
        w.cfg.max_daily_retry_portions = 5
        w.db = mock.Mock()
        w.db.portions_today.return_value = 0
        w.alerter = mock.Mock()
        w.latest_snapshot_path = Path("/nonexistent/latest.jpg")
        w.latest_state = {}
        seq = iter(outcomes)

        def verify(**kw):
            w.latest_state = {"outcome": next(seq), "details": {}}
            return w.latest_state["outcome"] == "dispensed"

        w.verify_feed_event = mock.Mock(side_effect=verify)
        return w

    def test_cut_short_meal_is_never_retried(self):
        w = self._watchdog(["recording_cut_short"])
        w.run_scheduled_check(meal_portions=20)
        self.assertEqual(w.verify_feed_event.call_count, 1)
        self.assertTrue(w.verify_feed_event.call_args.kwargs["expect_dispense"])
        w.alerter.send_alert.assert_not_called()

    def test_unverifiable_retry_stops_further_retries(self):
        w = self._watchdog(["no_motor", "recording_cut_short", "dispensed"])
        w.run_scheduled_check(meal_portions=20)
        self.assertEqual(w.verify_feed_event.call_count, 2)  # meal + one retry, no second blind dispense

    def test_real_miss_still_retries(self):
        w = self._watchdog(["no_motor", "dispensed"])
        w.run_scheduled_check(meal_portions=20)
        self.assertEqual(w.verify_feed_event.call_count, 2)
        self.assertEqual(w.alerter.send_alert.call_args.kwargs["event"], "retry_succeeded")


class TestMissedFeedCounter(unittest.TestCase):
    def test_cut_short_rows_neither_count_nor_reset(self):
        from watchdog.db import KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "t.db"))

            def log(o):
                db.log_feed_event("scheduled_verify", "cloud", "cloud", 1, o == "dispensed", d.is_clog(o, o == "dispensed"),
                                  outcome=o)

            log("dispensed")
            log("recording_cut_short")
            self.assertEqual(db.consecutive_missed_feeds(), 0)
            log("motor_no_kibble")
            log("recording_cut_short")
            log("camera_unavailable")
            self.assertEqual(db.consecutive_missed_feeds(), 1)
            log("no_motor")
            self.assertEqual(db.consecutive_missed_feeds(), 2)


if __name__ == "__main__":
    unittest.main()
