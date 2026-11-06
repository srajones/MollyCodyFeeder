"""Regression tests (stdlib unittest only — no extra deps).

Run:  .venv/bin/python -m unittest discover -s tests -v

Media-based tests use the user-labeled clips in captures/review_clips/ and are skipped
automatically when the media files are not present (they are git-ignored).
"""
import json
import os

import numpy as np
import struct
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WEBHOOK_SECRET", "")

from watchdog import molly_client as mc  # noqa: E402
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402
from watchdog.schedule_decoder import (  # noqa: E402
    decode_molly_schedule,
    encode_molly_schedule,
    update_molly_schedule_slot,
)

CLIPS = ROOT / "tests" / "fixtures" / "labeled_clips"
LABELS = CLIPS / "training_labels.json"

# Live DP 207 value captured from the real feeder (4 daily meals + 1 disabled one-off slot)
LIVE_SCHEDULE = "7f07002010000007f11002010000007f16002010000007f1900171000000000908100000000"


def _reviewed_clips():
    if not LABELS.exists():
        return []
    return [r for r in json.loads(LABELS.read_text()) if r.get("user_reviewed")]


class TestTalkFraming(unittest.TestCase):
    def test_talk_frame_layout(self):
        payload = b"\xd5" * 320
        frame = mc._imm_build_talk_frame(0, 0, 123456789, payload, req_id=7)
        self.assertEqual(len(frame), 32 + 320)
        magic, ch, codec, rate, bits, chans, ts, ln = struct.unpack_from("<IIIIIIII", frame, 0)
        self.assertEqual(magic, 0x12345678)
        self.assertEqual(ch, 7, "channel field must equal the talk-open reqId or the camera drops audio")
        self.assertEqual(codec, 0x86)
        self.assertEqual((rate, bits, chans), (0, 1, 0))
        self.assertEqual(ts, 123456789)
        self.assertEqual(ln, 320)
        self.assertEqual(frame[32:], payload)

    def test_talk_payload_is_mulaw(self):
        # On-device loopback: a 700 Hz tone came back at 699 Hz with mu-law, as 2.1 kHz static with A-law
        from watchdog.molly.speaker import SpeakerMixin

        self.assertEqual(SpeakerMixin.TALK_FFMPEG_FORMAT, "mulaw")
        self.assertEqual(SpeakerMixin.TALK_SILENCE_BYTE, b"\xff")

    def test_ctrl_builder(self):
        raw = mc._imm_build_ctrl(7, 8, struct.pack("<II", 0, 0))
        self.assertEqual(raw[:4], struct.pack("<I", 0x12345678))
        self.assertEqual(struct.unpack_from("<IIII", raw, 4), (7, 0, 8, 8))


class TestSchedule(unittest.TestCase):
    def test_decode_live(self):
        meals = decode_molly_schedule(LIVE_SCHEDULE)
        self.assertEqual([m.time_str for m in meals[:4]], ["07:00", "11:00", "16:00", "19:00"])
        self.assertEqual([m.portions for m in meals[:4]], [20, 20, 20, 17])
        self.assertTrue(all(m.enabled for m in meals[:4]))
        self.assertFalse(meals[4].enabled)

    def test_roundtrip(self):
        self.assertEqual(encode_molly_schedule(decode_molly_schedule(LIVE_SCHEDULE)), LIVE_SCHEDULE)

    def test_update_slot_only_touches_that_slot(self):
        new_raw = update_molly_schedule_slot(LIVE_SCHEDULE, slot=2, portions=5)
        meals = decode_molly_schedule(new_raw)
        self.assertEqual(len(new_raw), len(LIVE_SCHEDULE))
        self.assertEqual(meals[1].portions, 5)
        old = decode_molly_schedule(LIVE_SCHEDULE)
        for i in (0, 2, 3, 4):
            self.assertEqual(meals[i].to_dict(), old[i].to_dict())


@unittest.skipUnless(_reviewed_clips(), "no labeled clips available")
class TestAnalyzerOnLabeledClips(unittest.TestCase):
    """Every user-reviewed clip must keep its ground-truth verdict (no regressions)."""

    @classmethod
    def setUpClass(cls):
        cls.analyzer = BowlAnalyzer(WatchdogConfig())
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_audio_verdicts(self):
        checked = 0
        for rec in _reviewed_clips():
            wav = CLIPS / f"{rec['clip_id']}.wav"
            if not wav.exists():
                continue
            res = self.analyzer.analyze_dispense_audio(str(wav))
            with self.subTest(clip=rec["clip_id"]):
                self.assertTrue(res.motor_detected, "motor ran in every labeled clip")
                self.assertEqual(res.kibble_dispensed_audio, bool(rec["feed_success"]), res.diagnosis)
            checked += 1
        if not checked:
            self.skipTest("labeled WAVs not present")

    def test_vision_safety_rules(self):
        """Vision is confirm-only: never 'empty', never confirms food on a clog; still sees new food where it can."""
        checked = 0
        for rec in _reviewed_clips():
            before = CLIPS / f"{rec['clip_id']}_before.jpg"
            after = CLIPS / f"{rec['clip_id']}_after.jpg"
            if not (before.exists() and after.exists()):
                continue
            res = self.analyzer.analyze_bowl_visual(
                str(after), str(before), annotated_output_path=str(Path(self.tmp.name) / "ann.jpg")
            )
            with self.subTest(clip=rec["clip_id"]):
                self.assertFalse(res.bowl_empty, res.diagnosis)
                if rec.get("is_clog"):
                    self.assertFalse(res.food_increased, f"vision falsely confirmed food on a clog: {res.diagnosis}")
                if rec["clip_id"] in ("20261029_174950_feed_1p", "20261029_175236_feed_1p"):
                    self.assertTrue(res.food_increased, res.diagnosis)
            checked += 1
        if not checked:
            self.skipTest("labeled images not present")

    def test_full_verdict_matches_owner_labels(self):
        """Audio + vision decision equals the owner's label on every labeled clip."""
        from watchdog.service.decision import decide_passed

        checked = 0
        for rec in _reviewed_clips():
            wav = CLIPS / f"{rec['clip_id']}.wav"
            before = CLIPS / f"{rec['clip_id']}_before.jpg"
            after = CLIPS / f"{rec['clip_id']}_after.jpg"
            if not (wav.exists() and after.exists()):
                continue
            a = self.analyzer.analyze_dispense_audio(str(wav))
            v = self.analyzer.analyze_bowl_visual(
                str(after), str(before) if before.exists() else None,
                annotated_output_path=str(Path(self.tmp.name) / "ann.jpg"),
            )
            with self.subTest(clip=rec["clip_id"]):
                self.assertEqual(decide_passed(a, v, 0, True), bool(rec["feed_success"]), a.diagnosis)
            checked += 1
        if not checked:
            self.skipTest("labeled clips not present")

def _bare_watchdog(tmpdir: str):
    """PuppyFeederWatchdog without cloud/DB side effects."""
    from unittest import mock

    import watchdog.main as wm

    w = wm.PuppyFeederWatchdog.__new__(wm.PuppyFeederWatchdog)
    w.cfg = WatchdogConfig()
    w.molly = mock.Mock()
    w.molly.get_schedule.return_value = ("", [])
    w.db = mock.Mock()
    w.db.ok_video_clips_older_than.return_value = []
    w.work_dir = Path(tmpdir)
    w.latest_snapshot_path = Path(tmpdir) / "latest_annotated.jpg"
    w.latest_state = {"status": "idle"}
    w._last_fresh_snapshot = 0.0
    w._busy = __import__("threading").Lock()
    w.alerter = mock.Mock()
    return w


class TestWatchdogOps(unittest.TestCase):
    def test_meal_preroll_wraps_midnight(self):
        import schedule

        with tempfile.TemporaryDirectory() as td:
            w = _bare_watchdog(td)
            w.cfg.feed_schedule_times = ["00:00", "07:30"]
            w.cfg.meal_preroll_seconds = 40
            w.cfg.pre_meal_online_check_seconds = 0
            w.sync_schedule_jobs()
            ats = sorted(str(j.at_time) for j in schedule.get_jobs("meal_checks"))
            schedule.clear("meal_checks")
        self.assertEqual(ats, ["07:29:20", "23:59:20"])

    def test_webhook_auth(self):
        import socket
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as td:
            w = _bare_watchdog(td)
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            w.cfg.webhook_listen_host, w.cfg.webhook_listen_port = "127.0.0.1", port
            w.cfg.webhook_secret = "s3cret-token"
            w.start_webhook_server()
            base = f"http://127.0.0.1:{port}"

            def status(path, token=None, method="GET"):
                req = urllib.request.Request(base + path, method=method, data=b"{}" if method == "POST" else None)
                if token is not None:
                    req.add_header("Authorization", f"Bearer {token}")
                try:
                    return urllib.request.urlopen(req, timeout=5).status
                except urllib.error.HTTPError as e:
                    return e.code

            self.assertEqual(status("/health"), 200)  # liveness stays open
            self.assertEqual(status("/history"), 401)
            self.assertEqual(status("/history", "wrong"), 401)
            self.assertEqual(status("/history", "xx s3cret-token xx"), 401)  # old substring bug
            w.db.get_stats.return_value = {}
            w.db.get_recent_events.return_value = []
            w.db.get_recent_actions.return_value = []
            self.assertEqual(status("/history", "s3cret-token"), 200)
            self.assertEqual(status("/webhook/nope", "bad", method="POST"), 401)
            w.molly.get_status.assert_not_called()  # /health must not hit the cloud


class TestV02(unittest.TestCase):
    def test_assess_dispense_flags(self):
        from watchdog.analyzer import AudioAnalysisResult, assess_dispense

        def audio(motor, peaks, ok=True):
            return AudioAnalysisResult(True, peaks, ok, 30.0, 10.0, "", motor, round(peaks / motor, 2))

        self.assertFalse(any(v for k, v in assess_dispense(audio(12.0, 160), 5).items() if k != "expected_motor_sec"))
        self.assertTrue(assess_dispense(audio(4.0, 50), 10)["short_run"])  # 10p should run ~23 s
        self.assertFalse(assess_dispense(audio(4.0, 50), 10, recording_truncated=True)["short_run"])
        self.assertTrue(assess_dispense(audio(34.8, 111), 15)["low_flow"])  # 3.2 impacts/s
        self.assertFalse(assess_dispense(audio(2.5, 9, ok=False), 1)["low_flow"])  # clog handled elsewhere

    def test_hopper_and_baselines(self):
        from watchdog.db import KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "t.db"))
            self.assertFalse(db.hopper_status(240)["known"])
            db.record_refill(100)
            for p, motor, rate in ((5, 11.5, 13.0), (5, 12.0, 14.0), (1, 2.6, 13.5)):
                db.log_feed_event("manual_dispense_verify", "cloud", "cloud", p, True, False,
                                  motor_detected=True, outcome="dispensed", motor_duration_sec=motor, kibble_rate=rate)
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 20, False, True, motor_detected=False,
                              outcome="camera_unavailable")  # not counted: motor never confirmed
            hop = db.hopper_status(240)
            self.assertEqual((hop["portions_used"], hop["portions_left"], hop["percent_left"]), (11, 89, 89.0))
            base = db.learned_baselines()
            self.assertAlmostEqual(base["kibble_rate"], 13.5)
            self.assertAlmostEqual(base["motor_sec_per_portion"], 2.4, places=1)
            self.assertEqual(db.portions_today("manual_dispense_verify"), 11)

    def test_alert_cooldown(self):
        from watchdog.alerter import HouseholdAlerter

        cfg = WatchdogConfig()
        cfg.discord_webhook_url = cfg.telegram_bot_token = cfg.ntfy_topic_url = cfg.ha_webhook_url = ""
        cfg.alert_cooldown_minutes = 20
        a = HouseholdAlerter(cfg)
        self.assertTrue(a.send_alert("t", "m", event="x"))
        self.assertFalse(a.send_alert("t", "m", event="x"))
        self.assertTrue(a.send_alert("t", "m", event="y"))
        self.assertTrue(a.send_alert("t", "m", event="x", cooldown_minutes=0))

    def test_capture_never_double_dispenses(self):
        from unittest import mock

        c = mc.MollyFeederClient.__new__(mc.MollyFeederClient)
        calls = {"n": 0}

        def trigger():
            calls["n"] += 1

        def fail_after_trigger(coro):
            coro.close()
            trigger_wrapped()  # simulate: dispense fired, then the session died
            raise RuntimeError("ICE timeout")

        def fail_before_trigger(coro):
            coro.close()
            raise RuntimeError("answer timeout")

        with mock.patch("time.sleep"):
            # failure AFTER the feed command: no retry, no fallback dispense
            trigger_wrapped = None

            def capture_run(coro):
                nonlocal trigger_wrapped
                trigger_wrapped = coro.cr_frame.f_locals["trigger_fn"]
                return fail_after_trigger(coro)

            c._run_session = capture_run
            res = c.capture_av_clip(5, "/tmp/x.mp4", "/tmp/x.wav", trigger_fn=trigger)
            self.assertEqual(calls["n"], 1)
            self.assertFalse(res["dispensed_unverified"])

            # failure BEFORE the feed command (both attempts): fallback dispenses exactly once
            calls["n"] = 0
            c._run_session = fail_before_trigger
            res = c.capture_av_clip(5, "/tmp/x.mp4", "/tmp/x.wav", trigger_fn=trigger)
            self.assertEqual(calls["n"], 1)
            self.assertTrue(res["dispensed_unverified"])


class TestV03(unittest.TestCase):
    def _a(self, kibble, motor=True):
        from watchdog.analyzer import AudioAnalysisResult
        return AudioAnalysisResult(motor, 40 if kibble else 5, kibble, 30.0, 10.0, "", 3.0 if motor else 0.0)

    def _v(self, **kw):
        from watchdog.analyzer import VisualAnalysisResult
        base = dict(before_fill_ratio=0.1, after_fill_ratio=0.1, fill_delta_ratio=0.0, frame_diff_ratio=0.0,
                    bowl_empty=False, food_increased=False, food_present_cv=True, food_present_llm=None,
                    llm_reasoning="", annotated_image_path="", diagnosis="")
        base.update(kw)
        return VisualAnalysisResult(**base)

    def test_decision_matrix(self):
        """Owner's policy: the kibble 'ting' decides; vision never vetoes it, and only confirms food when there's no audio."""
        from watchdog.main import decide_passed as d
        leftovers = self._v()
        # No ting on an expected feed = miss, even with leftover food visible
        self.assertFalse(d(self._a(False), leftovers, 0, expect_dispense=True))
        self.assertTrue(d(self._a(True), leftovers, 0, expect_dispense=True))
        # Ting heard -> pass even if vision can't see food (lights on / IR / dog)
        self.assertTrue(d(self._a(True), self._v(food_present_cv=False), 0, True))
        self.assertTrue(d(self._a(True), self._v(is_infrared_mode=True, food_present_cv=False), 0, True))
        # Audio present without the ting: vision (noisy IR / dog detectors) can't flip it to a pass ...
        self.assertFalse(d(self._a(False), self._v(food_increased=True), 0, True))
        # ... unless VISION_CAN_OVERRIDE_AUDIO=true (legacy behaviour)
        self.assertTrue(d(self._a(False), self._v(food_increased=True), 0, True, vision_can_override_audio=True))
        # Hardware fault always fails
        self.assertFalse(d(self._a(True), leftovers, 2, True))
        # No audio: vision must confirm new food; on-demand check accepts food in bowl
        self.assertTrue(d(None, self._v(food_increased=True), 0, True))
        self.assertFalse(d(None, leftovers, 0, True))
        self.assertTrue(d(None, leftovers, 0, expect_dispense=False))

    def test_schedule_change_alerts(self):
        from watchdog.schedule_decoder import decode_molly_schedule, encode_molly_schedule

        with tempfile.TemporaryDirectory() as td:
            w = _bare_watchdog(td)
            meals = decode_molly_schedule(LIVE_SCHEDULE)
            w._notice_schedule_change(LIVE_SCHEDULE, meals)
            w.alerter.send_alert.assert_not_called()  # first sync = baseline
            meals[1].enabled = False
            w._notice_schedule_change(encode_molly_schedule(meals), meals)
            kw = w.alerter.send_alert.call_args.kwargs
            self.assertEqual(kw["event"], "schedule_changed")
            self.assertIn("11:00", kw["message"])
            self.assertTrue(kw["urgent"])
            for m in meals:
                m.enabled = False
            w._notice_schedule_change(encode_molly_schedule(meals), meals)
            self.assertEqual(w.alerter.send_alert.call_args.kwargs["event"], "schedule_empty")

    def test_consecutive_missed_feeds(self):
        from watchdog.db import KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "t.db"))
            log = lambda o: db.log_feed_event("scheduled_verify", "cloud", "cloud", 1, o == "dispensed", False, outcome=o)
            log("motor_no_kibble"); log("dispensed")
            self.assertEqual(db.consecutive_missed_feeds(), 0)
            log("motor_no_kibble")
            self.assertEqual(db.consecutive_missed_feeds(), 1)
            log("camera_unavailable")  # unverified runs neither count nor reset
            log("no_motor")
            self.assertEqual(db.consecutive_missed_feeds(), 2)

    def test_bark_before_meal_not_mistaken_for_motor(self):
        import wave

        src = CLIPS / "20261029_174950_feed_1p.wav"
        if not src.exists():
            self.skipTest("labeled clip not present")
        with wave.open(str(src)) as w:
            sr = w.getframerate()
            real = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        voice = (np.random.default_rng(0).normal(0, 0.05, int(1.5 * sr)) * 32767).astype(np.int16)
        sig = np.concatenate([np.zeros(5 * sr, np.int16), voice, np.zeros(15 * sr, np.int16), real])
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "s.wav"
            with wave.open(str(out), "wb") as o:
                o.setnchannels(1)
                o.setsampwidth(2)
                o.setframerate(sr)
                o.writeframes(sig.tobytes())
            res = BowlAnalyzer(WatchdogConfig()).analyze_dispense_audio(str(out))
        self.assertTrue(res.kibble_dispensed_audio, res.diagnosis)

    def test_dp_retry_only_when_never_sent(self):
        import requests

        self.assertTrue(mc._never_sent(requests.exceptions.ConnectTimeout("x")))
        self.assertTrue(mc._never_sent(requests.exceptions.ConnectionError("NewConnectionError: refused")))
        self.assertFalse(mc._never_sent(requests.exceptions.ReadTimeout("x")))
        self.assertFalse(mc._never_sent(requests.exceptions.ConnectionError("Connection aborted, RemoteDisconnected")))


class TestRetention(unittest.TestCase):
    """Footage expires per policy; metrics stay in SQLite and are exported to JSONL before anything is deleted."""

    def _setup(self, td, **cfg_overrides):
        import sqlite3

        from watchdog.db import KibbleDatabase
        from watchdog.service.retention import RetentionManager

        caps = Path(td) / "caps"
        caps.mkdir()
        cfg = WatchdogConfig()
        cfg.keep_footage, cfg.capture_retention_days, cfg.keep_failed_footage_days = True, 14, 90
        cfg.video_keep_days_ok, cfg.capture_max_mb, cfg.db_retention_days = 0, 0, 0
        cfg.export_ml_on_prune, cfg.ml_export_path = True, str(Path(td) / "ml.jsonl")
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
        db = KibbleDatabase(str(Path(td) / "t.db"))
        protected = caps / "latest_annotated.jpg"
        protected.write_bytes(b"keep")
        rm = RetentionManager(cfg, db, caps, protected=[protected])

        def event(days_old, success, clog, outcome, portions=5):
            files = {}
            for col, suf in (("video_clip", "mp4"), ("audio_clip", "wav"), ("after_image", "jpg")):
                f = caps / f"e{days_old}_{outcome}.{suf}"
                f.write_bytes(b"x" * 100)
                files[col] = str(f)
            eid = db.log_feed_event("scheduled_verify", "cloud", "cloud", portions, success, clog, outcome=outcome,
                                    kibble_peaks=42, after_fill_pct=12.5, motor_duration_sec=11.0,
                                    details={"audio": {"ting_count": 60, "ting_rate_per_sec": 5.4}}, **files)
            con = sqlite3.connect(db.db_path)
            con.execute("UPDATE feed_events SET timestamp = datetime('now', 'localtime', ?) WHERE id = ?",
                        (f"-{days_old} days", eid))
            con.commit()
            con.close()
            return eid, files

        return cfg, db, rm, caps, protected, event

    def test_capture_retention_keeps_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, db, rm, caps, protected, event = self._setup(td)
            old_ok, old_files = event(20, True, False, "dispensed")
            new_ok, new_files = event(2, True, False, "dispensed")
            clog_30, clog30_files = event(30, False, True, "motor_no_kibble")
            clog_100, clog100_files = event(100, False, True, "motor_no_kibble")
            (caps / "review_clips").mkdir()
            (caps / "review_clips" / "keep.mp4").write_bytes(b"x")
            rm.run()
            self.assertFalse(any(Path(f).exists() for f in old_files.values()))      # 20 d > 14 d
            self.assertTrue(all(Path(f).exists() for f in new_files.values()))       # 2 d
            self.assertTrue(all(Path(f).exists() for f in clog30_files.values()))    # failed: 90 d keep
            self.assertFalse(any(Path(f).exists() for f in clog100_files.values()))  # 100 d > 90 d
            self.assertTrue(protected.exists() and (caps / "review_clips" / "keep.mp4").exists())
            row = db.get_event(old_ok)
            self.assertIsNotNone(row["media_purged_at"])
            self.assertIsNone(row["video_clip"])
            self.assertEqual((row["portions"], row["kibble_peaks"], row["after_fill_pct"]), (5, 42, 12.5))
            lines = [json.loads(x) for x in Path(cfg.ml_export_path).read_text().splitlines()]
            self.assertEqual({r["event_id"] for r in lines}, {old_ok, clog_100})
            rec = next(r for r in lines if r["event_id"] == clog_100)
            self.assertEqual((rec["portions"], rec["clog_detected"], rec["ting_rate_per_sec"]), (5, True, 5.4))
            rm.run()  # idempotent: nothing exported twice
            self.assertEqual(len(Path(cfg.ml_export_path).read_text().splitlines()), 2)

    def test_keep_footage_false_purges_right_after_analysis(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, db, rm, caps, protected, event = self._setup(td, keep_footage=False)
            eid, files = event(0, True, False, "dispensed")
            rm.purge_after_analysis(eid)
            self.assertFalse(any(Path(f).exists() for f in files.values()))
            self.assertEqual(db.get_event(eid)["portions"], 5)
            self.assertEqual(len(Path(cfg.ml_export_path).read_text().splitlines()), 1)

    def test_retention_zero_keeps_forever_and_db_retention(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, db, rm, caps, protected, event = self._setup(td, capture_retention_days=0, keep_failed_footage_days=0)
            eid, files = event(400, True, False, "dispensed")
            rm.run()
            self.assertTrue(all(Path(f).exists() for f in files.values()))
            cfg.db_retention_days = 365
            stats = rm.run()
            self.assertEqual(stats["rows_deleted"], 1)
            self.assertIsNone(db.get_event(eid))
            self.assertEqual(json.loads(Path(cfg.ml_export_path).read_text().splitlines()[0])["event_id"], eid)

    def test_size_cap_and_ok_video_tier(self):
        with tempfile.TemporaryDirectory() as td:
            cfg, db, rm, caps, protected, event = self._setup(td, capture_retention_days=0, video_keep_days_ok=3)
            eid, files = event(5, True, False, "dispensed")
            rm.run()
            self.assertFalse(Path(files["video_clip"]).exists())  # good-feed MP4 tier
            self.assertTrue(Path(files["audio_clip"]).exists())
            orphan = caps / "orphan.jpg"
            orphan.write_bytes(b"x" * 3_000_000)
            cfg.capture_max_mb = 1
            rm.run()
            self.assertFalse(orphan.exists())
            self.assertTrue(protected.exists())

    def test_export_ml_bundle_and_first_install_defaults(self):
        import subprocess
        import tarfile
        import time
        from watchdog.db import KibbleDatabase

        expected_defaults = {
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
        }
        example_text = (ROOT / ".env.example").read_text()
        for k, v in expected_defaults.items():
            self.assertIn(f"{k}={v}", example_text)

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            (proj / "data").mkdir()
            (proj / "captures" / "review_clips").mkdir(parents=True)
            (proj / ".env").write_text(
                "MOLLY_PASS=top_secret_password\nWEBHOOK_SECRET=top_secret_token\n"
                "KEEP_FOOTAGE=true\nCAPTURE_RETENTION_DAYS=30\nDB_RETENTION_DAYS=0\n"
            )
            db = KibbleDatabase(str(proj / "data" / "kibble_history.db"))
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 4, True, False, outcome="dispensed")
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 4, False, True, outcome="motor_no_kibble")
            (proj / "data" / "ml_events.jsonl").write_text('{"event_id": 1}\n')
            (proj / "captures" / "review_clips" / "training_labels.json").write_text('{"clip1": {}}\n')
            recent_clip = proj / "captures" / "recent_dispense.mp4"
            recent_clip.write_bytes(b"mp4data")
            old_clip = proj / "captures" / "old_dispense.mp4"
            old_clip.write_bytes(b"oldmp4")
            old_ts = time.time() - 45 * 86400
            os.utime(old_clip, (old_ts, old_ts))

            env = {**os.environ, "POOR_PUPPY_DIR": str(proj)}
            script = str(ROOT / "scripts" / "export_ml_bundle.sh")

            # 1) Default export includes media + DB + JSONL + labels
            r1 = subprocess.run([script], env=env, capture_output=True, text=True, check=True)
            self.assertIn("scp ", r1.stdout)
            archives = list((proj / "exports").glob("poor-puppy-ml-*.tar.gz"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
            with tarfile.open(archives[0], "r:gz") as tf:
                names = tf.getnames()
                manifest_member = next(n for n in names if n.endswith("/manifest.json"))
                manifest = json.loads(tf.extractfile(manifest_member).read().decode())
                self.assertEqual(manifest["feed_events"]["total"], 2)
                self.assertEqual(manifest["feed_events"]["success"], 1)
                self.assertEqual(manifest["feed_events"]["clog"], 1)
                self.assertEqual(manifest["ml_events_jsonl_rows"], 1)
                self.assertNotIn("MOLLY_PASS", json.dumps(manifest))
                self.assertNotIn("top_secret", json.dumps(manifest))
                self.assertTrue(any(n.endswith("recent_dispense.mp4") for n in names))
                self.assertTrue(any(n.endswith("old_dispense.mp4") for n in names))

            # 2) --no-media (metrics only)
            no_media_out = proj / "exports" / "nomedia.tar.gz"
            subprocess.run([script, "--no-media", "--out", str(no_media_out)],
                           env=env, capture_output=True, text=True, check=True)
            with tarfile.open(no_media_out, "r:gz") as tf:
                names = tf.getnames()
                self.assertFalse(any(n.endswith(".mp4") for n in names))
                self.assertTrue(any(n.endswith("kibble_history.db") for n in names))

            # 3) --days 30 --out custom.tar.gz (media by default, filtered by age)
            custom_out = proj / "exports" / "custom.tar.gz"
            subprocess.run([script, "--days", "30", "--out", str(custom_out)],
                           env=env, capture_output=True, text=True, check=True)
            with tarfile.open(custom_out, "r:gz") as tf:
                names = tf.getnames()
                self.assertTrue(any(n.endswith("recent_dispense.mp4") for n in names))
                self.assertFalse(any(n.endswith("old_dispense.mp4") for n in names))


class TestNoFoodTimer(unittest.TestCase):
    def _w(self, td):
        from watchdog.db import KibbleDatabase

        w = _bare_watchdog(td)
        w.db = KibbleDatabase(str(Path(td) / "t.db"))
        w.cfg.no_food_alert_hours = 8
        w.analyzer = __import__("unittest").mock.Mock()
        w.molly.capture_snapshot.return_value = False
        return w

    def test_alerts_only_after_hours_without_food(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            w = self._w(td)
            self.assertIsNone(w.check_no_food_timer())  # fresh install: nothing to compare
            w.db.record_food_evidence("ting", "test")
            self.assertLess(w.check_no_food_timer(), 1)
            w.alerter.send_alert.assert_not_called()
            con = sqlite3.connect(w.db.db_path)
            con.execute("UPDATE food_observations SET timestamp = datetime('now', 'localtime', '-9 hours')")
            con.commit()
            con.close()
            self.assertGreaterEqual(w.check_no_food_timer(), 8.9)
            self.assertEqual(w.alerter.send_alert.call_args.kwargs["event"], "no_food_timer")

    def test_food_seen_in_snapshot_resets_timer(self):
        with tempfile.TemporaryDirectory() as td:
            w = self._w(td)
            w.molly.capture_snapshot.return_value = True
            w.analyzer.food_seen.return_value = True
            self.assertLess(w.check_no_food_timer(), 1)
            self.assertEqual(w.db.last_food_evidence()["source"], "camera")

    def test_timer_mode_silences_per_feed_alerts(self):
        from watchdog.service.models import VerifyRequest

        v3 = TestV03()
        with tempfile.TemporaryDirectory() as td:
            v, molly, db, _ = TestV04Refactor()._verifier(td, v3._a(False), v3._v(food_present_cv=False))
            v.cfg.alert_mode = "no_food_hours"
            passed, state = v.run(VerifyRequest(trigger_dispense=True, portions=1))
            self.assertFalse(passed)
            v.alerter.send_alert.assert_not_called()
            molly.speak_tts.assert_not_called()

    def test_pp_settings_preserves_comments_and_secrets(self):
        import importlib.util

        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text("# keep me\nMOLLY_PASS=secret\nALERT_MODE=every_miss\n")
            os.environ["POOR_PUPPY_DIR"] = td
            spec = importlib.util.spec_from_file_location("pp", ROOT / "scripts" / "pp_settings.py")
            pp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pp)
            pp.write_env({"ALERT_MODE": "no_food_hours", "NO_FOOD_ALERT_HOURS": "6"})
            text = env.read_text()
            self.assertIn("# keep me", text)
            self.assertIn("MOLLY_PASS=secret", text)
            self.assertIn("ALERT_MODE=no_food_hours", text)
            self.assertIn("NO_FOOD_ALERT_HOURS=6", text)
            self.assertEqual(pp.shown("MOLLY_PASS", "supersecret"), "••••cret")
            with __import__("unittest").mock.patch.object(pp, "compose") as m_compose:
                m_compose.return_value = __import__("subprocess").CompletedProcess(
                    [], 0,
                    'log line\n{"status":"ok","outcome":"dispensed","food_dispensed":true,"clog_detected":false,'
                    '"db_event_id":7,"details":{"audio":{"motor_detected":true,"motor_duration_sec":2.7,'
                    '"kibble_impact_peaks":38,"ting_count":18,"ting_rate_per_sec":6.7},'
                    '"visual":{"before_fill_ratio":0.01,"after_fill_ratio":0.15,"fill_delta_ratio":0.14,'
                    '"food_present_cv":true},"annotated_image":"/tmp/poor_puppy_captures/a.jpg"}}',
                    "",
                )
                pp.view_schedule()
                m_compose.assert_called_with("exec", "feeder-watchdog", "python", "feeder_cli.py", "schedule", "list")
                pp.feed_no_verify("3")
                m_compose.assert_called_with("exec", "feeder-watchdog", "python", "feeder_cli.py", "feed", "3")
                pp.feed_and_verify("2")
                m_compose.assert_called_with("exec", "-T", "feeder-watchdog", "python", "feeder_cli.py", "feed", "2", "--verify", capture=True)
                summary = pp.summarize_verify_output(m_compose.return_value.stdout, "")
                self.assertIn("status=ok | outcome=dispensed | food_dispensed=True", summary)
                self.assertIn("delta +14.0%", summary)
                self.assertIn("/tmp/poor_puppy_captures/a.jpg", summary)
                err_summary = pp.summarize_verify_output("not json", "line1\nTraceback: boom")
                self.assertIn("Error tail:", err_summary)
                self.assertIn("Traceback: boom", err_summary)

                # Unconfigured TTS prints a clear message and does not call compose
                m_compose.reset_mock()
                pp.tts_test(env={}, phrase="Test phrase")
                m_compose.assert_not_called()

                # Configured TTS speaks phrase after '--' and speaks last alert / unverified summary honestly
                from watchdog.db import KibbleDatabase

                (Path(td) / "data").mkdir(exist_ok=True)
                db = KibbleDatabase(str(Path(td) / "data" / "kibble_history.db"))
                db.log_feed_event("manual_feed_unverified", "cloud", "cloud", 3, True, False, outcome="unverified", kibble_peaks=0)
                self.assertIn("(not camera-verified)", pp.last_alert_summary_text(Path(td)))

                db.log_feed_event("scheduled_verify", "cloud", "cloud", 5, False, True, outcome="motor_no_kibble")
                tts_env = {
                    "MOLLY_USER": "u@x.com",
                    "MOLLY_PASS": "secret",
                    "TTS_VOLUME": "2.5",
                    "TTS_VOICE": "en-US-AriaNeural",
                }
                pp.speak_custom_tts(env=tts_env, phrase="-hello leading dash!")
                m_compose.assert_called_with(
                    "exec", "-T", "feeder-watchdog", "python", "feeder_cli.py",
                    "say", "--volume", "2.5", "--voice", "en-US-AriaNeural", "--repeat", "1", "--", "-hello leading dash!"
                )
                pp.tts_test(env=tts_env, phrase="Hello puppy")
                m_compose.assert_called_with(
                    "exec", "-T", "feeder-watchdog", "python", "feeder_cli.py",
                    "say", "--volume", "2.5", "--voice", "en-US-AriaNeural", "--repeat", "1", "--", "Hello puppy"
                )
                pp.speak_last_alert(env=tts_env)
                last_call = m_compose.call_args.args
                self.assertEqual(
                    last_call[:12],
                    ("exec", "-T", "feeder-watchdog", "python", "feeder_cli.py", "say", "--volume", "2.5", "--voice", "en-US-AriaNeural", "--repeat", "1"),
                )
                self.assertEqual(last_call[12], "--")
                self.assertIn("motor no kibble", last_call[13])

                # Speaker failure + HA fallback messaging
                m_compose.reset_mock()
                m_compose.side_effect = [
                    __import__("subprocess").CompletedProcess([], 2, "", "speaker fail"),
                    __import__("subprocess").CompletedProcess([], 0, "", ""),
                ]
                pp.tts_test(env={**tts_env, "HA_WEBHOOK_URL": "https://ha.example/api/webhook/x"}, phrase="Fallback test")
                self.assertEqual(m_compose.call_count, 2)
                m_compose.side_effect = None
            del os.environ["POOR_PUPPY_DIR"]

            # Test update-pp pre-flight guard + dirty working tree check + non-destructive .env backfill
            import subprocess

            updater = str(ROOT / "update-pp")
            with tempfile.TemporaryDirectory() as empty_td:
                r_fail = subprocess.run([updater, "--no-pull"], env={**os.environ, "POOR_PUPPY_DIR": empty_td},
                                        capture_output=True, text=True)
                self.assertNotEqual(r_fail.returncode, 0)
                self.assertIn("Refusing to run", r_fail.stderr)

            (Path(td) / "captures").mkdir(exist_ok=True)
            (Path(td) / ".env.example").write_text("NEW_KEY_FROM_EXAMPLE=42\nMOLLY_PASS=blank\n")
            r_ok = subprocess.run(
                [updater, "--no-pull"],
                env={**os.environ, "POOR_PUPPY_DIR": td, "PP_SKIP_DOCKER": "1"},
                capture_output=True, text=True, check=True,
            )
            self.assertIn("Update complete!", r_ok.stdout)
            updated_env = env.read_text()
            self.assertIn("MOLLY_PASS=secret", updated_env)  # never clobbered
            self.assertIn("NEW_KEY_FROM_EXAMPLE=42", updated_env)  # backfilled
            self.assertIn("KEEP_FOOTAGE=true", updated_env)

            # Dirty git tree check in update-pp (without --no-pull)
            subprocess.run(["git", "init"], cwd=td, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=td, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=td, capture_output=True, check=True)
            tracked = Path(td) / "tracked.txt"
            tracked.write_text("v1\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=td, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=td, capture_output=True, check=True)
            tracked.write_text("dirty\n")
            r_dirty = subprocess.run(
                [updater, "--branch", "v0.5"],
                env={**os.environ, "POOR_PUPPY_DIR": td, "PP_SKIP_DOCKER": "1"},
                capture_output=True, text=True,
            )
            self.assertNotEqual(r_dirty.returncode, 0)
            self.assertIn("git stash", r_dirty.stderr)


class TestV04Refactor(unittest.TestCase):
    """End-to-end FeedVerifier pipeline with fake collaborators (no network, no camera)."""

    def _verifier(self, td, audio, visual, capture_ok=True):
        from unittest import mock

        from watchdog.service.verifier import FeedVerifier

        cfg = WatchdogConfig()
        cfg.camera_stream_url = ""
        cfg.eaten_check_minutes = 120
        cfg.tts_alert_on_clog = True
        molly = mock.Mock(mode="cloud")
        molly.get_status.return_value = {"food_weight_g": 10, "feed_abnormal": 0, "dps": {}}
        molly.dispense_food.return_value = (True, "cloud")

        def capture(**kw):
            if kw.get("trigger_fn"):
                kw["trigger_fn"]()
            if not capture_ok:
                return {"audio_ok": False, "after_ok": False}
            for k in ("output_wav", "before_jpg", "after_jpg"):
                Path(kw[k]).write_bytes(b"x")
            return {"audio_ok": True, "after_ok": True, "motor_window_sec": [2.0, 5.0], "recording_extended_sec": 0}

        molly.capture_av_clip.side_effect = capture
        molly.speak_tts.return_value = {"ok": True}
        analyzer = mock.Mock()
        analyzer.analyze_dispense_audio.return_value = audio
        analyzer.analyze_bowl_visual.return_value = visual
        db = mock.Mock()
        db.learned_baselines.return_value = {}
        db.log_feed_event.return_value = 42
        db.consecutive_missed_feeds.return_value = 2  # second feed in a row without the ting
        eaten = mock.Mock()
        v = FeedVerifier(cfg, molly, analyzer, db, mock.Mock(), Path(td), Path(td) / "latest.jpg",
                         on_meal_verified=eaten, after_event=mock.Mock())
        return v, molly, db, eaten

    def test_clog_pipeline(self):
        from watchdog.service.models import VerifyRequest

        v3 = TestV03()
        with tempfile.TemporaryDirectory() as td:
            v, molly, db, _ = self._verifier(td, v3._a(False), v3._v(food_present_cv=False))
            passed, state = v.run(VerifyRequest(trigger_dispense=True, portions=1))
            self.assertFalse(passed)
            self.assertEqual(state["outcome"], "motor_no_kibble")
            self.assertEqual(state["status"], "clog_detected")
            self.assertEqual(state["db_event_id"], 42)
            molly.dispense_food.assert_called_once_with(1)
            kw = db.log_feed_event.call_args.kwargs
            self.assertTrue(kw["clog_detected"])
            self.assertEqual((kw["event_type"], kw["portions"]), ("manual_dispense_verify", 1))
            v.alerter.send_alert.assert_called_once()
            self.assertIn("CLOG", v.alerter.send_alert.call_args.kwargs["title"])
            molly.speak_tts.assert_called_once()  # voice alert on the feeder
            v.after_event.assert_called_once()

    def test_verified_pipeline_schedules_eaten_check(self):
        from unittest import mock

        from watchdog.service.models import VerifyRequest

        v3 = TestV03()
        with tempfile.TemporaryDirectory() as td:
            v, molly, db, eaten = self._verifier(td, v3._a(True), v3._v(food_increased=True))
            with mock.patch("threading.Timer") as timer:
                # fake motor ran 3 s -> consistent with 1 portion (5 would correctly raise "stopped early")
                passed, state = v.run(VerifyRequest(expected_portions=1, expect_dispense=True))
            self.assertTrue(passed)
            self.assertEqual(state["outcome"], "dispensed")
            molly.dispense_food.assert_not_called()  # scheduled meal: the feeder dispenses itself
            self.assertEqual(db.log_feed_event.call_args.kwargs["portions"], 1)
            timer.assert_called_once()
            v.alerter.send_alert.assert_not_called()
            v.alerter.send_ha_webhook.assert_called_once()

    def test_first_miss_is_only_a_heads_up(self):
        from watchdog.service.models import VerifyRequest

        v3 = TestV03()
        with tempfile.TemporaryDirectory() as td:
            v, molly, db, _ = self._verifier(td, v3._a(False), v3._v(food_present_cv=False))
            db.consecutive_missed_feeds.return_value = 1
            passed, _ = v.run(VerifyRequest(trigger_dispense=True, portions=1))
            self.assertFalse(passed)
            kw = v.alerter.send_alert.call_args.kwargs
            self.assertEqual(kw["event"], "feed_no_ting_first")
            self.assertFalse(kw["urgent"])
            molly.speak_tts.assert_not_called()

    def test_food_in_bowl_means_dog_ok(self):
        from watchdog.service.models import VerifyRequest

        v3 = TestV03()
        with tempfile.TemporaryDirectory() as td:
            v, molly, db, _ = self._verifier(td, v3._a(False), v3._v(food_present_cv=True))
            passed, _ = v.run(VerifyRequest(trigger_dispense=True, portions=1))
            kw = v.alerter.send_alert.call_args.kwargs
            self.assertIn("dog OK", kw["title"])
            self.assertFalse(kw["urgent"])
            molly.speak_tts.assert_not_called()

    def test_camera_down_is_not_a_clog(self):
        from watchdog.service.models import VerifyRequest

        with tempfile.TemporaryDirectory() as td:
            v, molly, db, _ = self._verifier(td, None, None, capture_ok=False)
            passed, state = v.run(VerifyRequest(expected_portions=5, expect_dispense=True))
            self.assertFalse(passed)
            self.assertEqual(state["outcome"], "camera_unavailable")
            self.assertFalse(db.log_feed_event.call_args.kwargs["clog_detected"])
            self.assertEqual(v.alerter.send_alert.call_args.kwargs["event"], "camera_unavailable")
            molly.speak_tts.assert_not_called()

    def test_http_busy_and_refill(self):
        import json as _json
        import socket
        import urllib.error
        import urllib.request

        with tempfile.TemporaryDirectory() as td:
            w = _bare_watchdog(td)
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            w.cfg.webhook_listen_host, w.cfg.webhook_listen_port, w.cfg.webhook_secret = "127.0.0.1", port, "tok"
            server = w.start_webhook_server()

            def post(path, body):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=_json.dumps(body).encode(),
                                             method="POST", headers={"X-Webhook-Token": "tok"})
                try:
                    r = urllib.request.urlopen(req, timeout=5)
                    return r.status, _json.loads(r.read() or b"{}")
                except urllib.error.HTTPError as e:
                    return e.code, {}

            w.db.hopper_status.return_value = {"portions_left": 240}
            self.assertEqual(post("/webhook/refill", {"capacity_portions": 240})[0], 200)
            w.db.record_refill.assert_called_once()
            with w._busy:
                self.assertEqual(post("/webhook/check", {})[0], 409)
            server.shutdown()


class TestV05AuditHardening(unittest.TestCase):
    """Regression tests for the P0/P1/P2 audit fixes on branch v0.5."""

    def test_dockerfile_bookworm_pin_and_no_libvpx7(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("FROM python:3.11-slim-bookworm", dockerfile)
        self.assertNotIn("libvpx7", dockerfile)

    def test_ha_webhook_bool_and_ha_tts_bypass_gating(self):
        import importlib.util
        import subprocess
        from unittest import mock
        from watchdog.alerter import HouseholdAlerter

        cfg = WatchdogConfig()
        cfg.ha_webhook_url = "https://ha.example/api/webhook/super_secret_hook"
        cfg.ha_notify_on_success = False
        alerter = HouseholdAlerter(cfg)

        with mock.patch("watchdog.alerter.requests.post") as m_post:
            m_post.return_value = mock.Mock(status_code=200)
            # Non-clog regular webhook is skipped when ha_notify_on_success=False unless force=True
            self.assertFalse(alerter.send_ha_webhook("feed_verified", False, "T", "M", {}))
            m_post.assert_not_called()
            self.assertTrue(alerter.send_ha_webhook("test_webhook", False, "T", "M", {}, force=True))
            self.assertEqual(m_post.call_count, 1)

            # send_ha_tts always sends even when ha_notify_on_success=False, with a clean TTS payload
            m_post.reset_mock()
            self.assertTrue(alerter.send_ha_tts("Dinner is served!"))
            m_post.assert_called_once()
            payload = m_post.call_args.kwargs["json"]
            self.assertEqual(payload["event"], "tts_speak")
            self.assertEqual(payload["event_type"], "tts_speak")
            self.assertEqual(payload["message"], "Dinner is served!")
            self.assertEqual(payload["tts_text"], "Dinner is served!")
            self.assertNotIn("food_dispensed", payload)
            self.assertNotIn("clog_detected", payload)

            # HTTP 500 returns False
            m_post.return_value = mock.Mock(status_code=500)
            self.assertFalse(alerter.send_ha_tts("Fail test"))

        # End-to-end _speak_phrase fallback executing the real inline Python command against mocked requests.post
        spec = importlib.util.spec_from_file_location("pp_ha", ROOT / "scripts" / "pp_settings.py")
        pp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pp)

        def fake_compose(*args, capture=False):
            # First call is feeder_cli.py say -> simulate speaker failure (exit 1)
            if "feeder_cli.py" in args:
                return subprocess.CompletedProcess(args, 1, "", "speaker unreachable")
            # Ensure HA_WEBHOOK_URL is NEVER passed on argv (ps leak guard)
            for a in args:
                self.assertNotIn("https://ha.example", str(a))
            # Second call is python -c "...send_ha_tts..." -> run the exact snippet in-process with mocked requests.post
            idx = args.index("-c")
            code = args[idx + 1]
            cmd_args = list(args[idx + 2:])
            with mock.patch("watchdog.alerter.requests.post", return_value=mock.Mock(status_code=200)) as mp, \
                 mock.patch.dict(os.environ, {"HA_WEBHOOK_URL": "https://ha.example/api/webhook/h1", "HA_NOTIFY_ON_SUCCESS": "false"}):
                ns = {"__name__": "__main__"}
                with mock.patch.object(sys, "argv", ["-c", *cmd_args]):
                    try:
                        exec(code, ns)
                        rc = 0
                    except SystemExit as e:
                        rc = int(e.code or 0)
                self.assertEqual(mp.call_count, 1)
                return subprocess.CompletedProcess(args, rc, "", "")

        with mock.patch.object(pp, "compose", side_effect=fake_compose):
            ok = pp._speak_phrase(
                "Hello from HA fallback",
                {"MOLLY_USER": "u@x.com", "MOLLY_PASS": "p", "HA_WEBHOOK_URL": "https://ha.example/api/webhook/h1"},
            )
            self.assertTrue(ok)

    def test_secret_redaction_and_pp_settings_masking(self):
        import importlib.util
        import subprocess
        from watchdog.alerter import HouseholdAlerter

        cfg = WatchdogConfig()
        cfg.telegram_bot_token = "123456789:AAH_top_secret_tg_tok"
        cfg.ha_webhook_url = "https://ha.example/api/webhook/secret_ha_id"
        cfg.discord_webhook_url = "https://discord.com/api/webhooks/999888777/secret_disc_token_xyz"
        cfg.ntfy_topic_url = "https://ntfy.sh/secret_puppy_topic_99"
        cfg.heartbeat_url = "https://hc-ping.com/12345678-1234-1234-1234-123456789abc"
        alerter = HouseholdAlerter(cfg)

        # Test both full URLs AND urllib3 split-path error strings (where host and path are logged separately)
        leaked = (
            "Max retries exceeded with url: /bot123456789:AAH_top_secret_tg_tok/sendMessage "
            "| url: /api/webhook/secret_ha_id "
            "| url: /api/webhooks/999888777/secret_disc_token_xyz "
            "| url: /secret_puppy_topic_99 "
            "| url: /12345678-1234-1234-1234-123456789abc"
        )
        redacted = alerter._redact(leaked)
        self.assertNotIn("AAH_top_secret_tg_tok", redacted)
        self.assertNotIn("secret_ha_id", redacted)
        self.assertNotIn("secret_disc_token_xyz", redacted)
        self.assertNotIn("999888777/secret_disc_token_xyz", redacted)
        self.assertNotIn("secret_puppy_topic_99", redacted)
        self.assertNotIn("12345678-1234-1234-1234-123456789abc", redacted)

        # Verify `feeder_cli.py webhook` masks HA_WEBHOOK_URL and never prints the full secret URL
        r_wh = subprocess.run(
            [sys.executable, str(ROOT / "feeder_cli.py"), "webhook"],
            env={**os.environ, "HA_WEBHOOK_URL": "https://ha.example/api/webhook/super_secret_hook_id"},
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn("super_secret_hook_id", r_wh.stdout)
        self.assertIn("••••k_id", r_wh.stdout)

        spec = importlib.util.spec_from_file_location("pp_sec", ROOT / "scripts" / "pp_settings.py")
        pp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pp)
        for key in ("HA_WEBHOOK_URL", "NTFY_TOPIC_URL", "REVIEW_TOKEN", "HEARTBEAT_URL", "DISCORD_WEBHOOK_URL"):
            self.assertTrue(pp.shown(key, "https://example.com/very-secret-value").startswith("••••"))

    def test_last_alert_summary_wording_matrix_and_zero_wal_in_data(self):
        import importlib.util
        import sqlite3
        from watchdog.db import KibbleDatabase

        spec = importlib.util.spec_from_file_location("pp_mat", ROOT / "scripts" / "pp_settings.py")
        pp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pp)

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            data_dir = proj / "data"
            data_dir.mkdir()
            # 1) No DB file yet
            self.assertEqual(pp.last_alert_summary_text(proj), "No feed events recorded yet. Poor Puppy watchdog is online.")

            db_path = data_dir / "kibble_history.db"
            db = KibbleDatabase(str(db_path))
            # 2) Empty feed_events table
            self.assertEqual(pp.last_alert_summary_text(proj), "No feed events recorded yet. Poor Puppy watchdog is online.")

            # 3) Camera-verified feed with 0 audio peaks and >0% fill -> "Verified by camera", NOT "not camera-verified"
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False,
                              outcome="dispensed", kibble_peaks=0, after_fill_pct=18.4)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("Verified by camera (0 audio peaks, 18 percent bowl fill).", txt)
            self.assertNotIn("not camera-verified", txt)

            # 4) Camera-confirmed food when audio was missing (outcome='unverified', success=True, food_increased=True or fill>0)
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False,
                              outcome="unverified", kibble_peaks=0, after_fill_pct=14.0, food_increased=True)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("Verified by camera", txt)
            self.assertNotIn("not camera-verified", txt)

            # 5) success=True + 0 peaks + 0% fill (no food_increased) -> honest caveat, NOT "Verified by camera ... 0 percent"
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False,
                              outcome="dispensed", kibble_peaks=0, after_fill_pct=0.0, food_increased=False)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("marked successful, but 0 audio peaks and 0 percent bowl fill were recorded", txt)
            self.assertNotIn("Verified by camera", txt)

            # 6) Audio+camera-verified feed with >0 peaks
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 3, True, False,
                              outcome="dispensed", kibble_peaks=41, after_fill_pct=24.0)
            self.assertIn("Verified with 41 audio peaks and 24 percent bowl fill.", pp.last_alert_summary_text(proj))

            # 7) Unverified manual feed (success)
            db.log_feed_event("manual_feed_unverified", "cloud", "cloud", 4, True, False,
                              outcome="unverified", kibble_peaks=0)
            self.assertIn("dispensed 4 portions (not camera-verified).", pp.last_alert_summary_text(proj))

            # 8) Failed unverified manual feed (both portions>0 and portions=0 as logged by feeder_cli.py on failure)
            db.log_feed_event("manual_feed_unverified", "cloud", "cloud", 4, False, False,
                              outcome="unverified", kibble_peaks=0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("last unverified feed command", txt)
            self.assertIn("for 4 portions failed to send.", txt)
            db.log_feed_event("manual_feed_unverified", "cloud", "cloud", 0, False, False,
                              outcome="unverified", kibble_peaks=0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("last unverified feed command", txt)
            self.assertIn("failed to send.", txt)
            self.assertNotIn("bowl check", txt)
            self.assertNotIn("0 portions", txt)

            # 9) Verifier outcome='unverified' with clog_detected=True -> clog/missed-kibble wording, NOT "failed to send"
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, False, True,
                              outcome="unverified", kibble_peaks=0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("no kibble confirmed in bowl (clog or missed feed)", txt)
            self.assertNotIn("failed to send", txt)

            # 10) Scheduled feed with portions=0 + motor_no_kibble / clog -> MUST NOT become "bowl check empty"
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 0, False, True,
                              outcome="motor_no_kibble", kibble_peaks=3, after_fill_pct=0.0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("last feed", txt)
            self.assertIn("reported motor no kibble", txt)
            self.assertNotIn("bowl check", txt)
            self.assertNotIn("0 portions", txt)

            # 11) Standalone bowl check (food seen vs empty bowl)
            db.log_feed_event("bowl_check", "cloud", "cloud", 0, True, False,
                              outcome="bowl_check", kibble_peaks=0, after_fill_pct=21.0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("last bowl check", txt)
            self.assertIn("saw food in the bowl (21 percent fill).", txt)
            self.assertNotIn("0 portions", txt)

            db.log_feed_event("bowl_check", "cloud", "cloud", 0, False, False,
                              outcome="bowl_check", kibble_peaks=0, after_fill_pct=0.0)
            txt = pp.last_alert_summary_text(proj)
            self.assertIn("last bowl check", txt)
            self.assertIn("found the bowl empty (0 percent fill).", txt)
            self.assertNotIn("0 portions", txt)

            # 12) Camera unavailable on bowl check vs feed
            db.log_feed_event("bowl_check", "cloud", "cloud", 0, False, False,
                              outcome="camera_unavailable", kibble_peaks=0)
            self.assertIn("last bowl check", pp.last_alert_summary_text(proj))
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, False, False,
                              outcome="camera_unavailable", kibble_peaks=0)
            self.assertIn("for 2 portions could not be verified because the feeder camera was unreachable.",
                          pp.last_alert_summary_text(proj))

            # 13) Verify reading from host creates ZERO -wal/-shm files in data/
            for suf in ("-wal", "-shm"):
                p = Path(str(db_path) + suf)
                if p.exists():
                    p.unlink()
            before_files = set(data_dir.iterdir())
            summary = pp.last_alert_summary_text(proj)
            after_files = set(data_dir.iterdir())
            self.assertTrue(summary.startswith("Poor Puppy"))
            self.assertEqual(before_files, after_files, "Host read must never create -wal/-shm files in data/")

            # 14) Legacy DB without 'outcome' column does not crash
            legacy_proj = Path(td) / "legacy"
            (legacy_proj / "data").mkdir(parents=True)
            con = sqlite3.connect(str(legacy_proj / "data" / "kibble_history.db"))
            con.execute(
                "CREATE TABLE feed_events (id INTEGER PRIMARY KEY, timestamp TEXT, event_type TEXT, "
                "portions INTEGER, success BOOLEAN, clog_detected BOOLEAN)"
            )
            con.execute(
                "INSERT INTO feed_events (timestamp, event_type, portions, success, clog_detected) "
                "VALUES ('2026-10-30 12:00:00', 'scheduled_verify', 2, 1, 0)"
            )
            con.commit()
            con.close()
            self.assertIn("dispensed 2 portions", pp.last_alert_summary_text(legacy_proj))

    def test_export_ml_bundle_integrity_check_and_concurrent_writer(self):
        import sqlite3
        import subprocess
        import tarfile
        import threading
        from watchdog.db import KibbleDatabase

        script = str(ROOT / "scripts" / "export_ml_bundle.sh")

        # 1) Corrupt DB must fail export_ml_bundle.sh with non-zero exit code and NOT ship a corrupt archive
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            (proj / "data").mkdir()
            (proj / "captures").mkdir()
            (proj / "data" / "kibble_history.db").write_bytes(b"NOT A VALID SQLITE DATABASE HEADER" * 20)
            out_bad = proj / "exports" / "bad.tar.gz"
            r_bad = subprocess.run(
                [script, "--no-media", "--out", str(out_bad)],
                env={**os.environ, "POOR_PUPPY_DIR": str(proj)},
                capture_output=True, text=True,
            )
            self.assertNotEqual(r_bad.returncode, 0)
            self.assertIn("failed integrity check", r_bad.stderr)
            self.assertFalse(out_bad.exists())

        # 2) Concurrent WAL writer hammering kibble_history.db during export must produce a valid DB passing integrity_check
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td)
            (proj / "data").mkdir()
            (proj / "captures").mkdir()
            db = KibbleDatabase(str(proj / "data" / "kibble_history.db"))
            for i in range(10):
                db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False, outcome="dispensed")

            stop_flag = threading.Event()

            def writer():
                i = 0
                while not stop_flag.is_set():
                    db.log_feed_event("scheduled_verify", "cloud", "cloud", 1, True, False,
                                      outcome="dispensed", diagnosis=f"concurrent-{i}" * 15)
                    i += 1

            t = threading.Thread(target=writer, daemon=True)
            t.start()
            try:
                out_ok = proj / "exports" / "concurrent.tar.gz"
                subprocess.run(
                    [script, "--no-media", "--out", str(out_ok)],
                    env={**os.environ, "POOR_PUPPY_DIR": str(proj)},
                    capture_output=True, text=True, check=True,
                )
            finally:
                stop_flag.set()
                t.join(timeout=5)

            with tarfile.open(out_ok, "r:gz") as tf:
                db_member = next(n for n in tf.getnames() if n.endswith("/data/kibble_history.db"))
                extracted_db = proj / "extracted.db"
                extracted_db.write_bytes(tf.extractfile(db_member).read())
            con = sqlite3.connect(str(extracted_db))
            chk = con.execute("PRAGMA integrity_check").fetchall()
            cnt = con.execute("SELECT COUNT(*) FROM feed_events").fetchone()[0]
            con.close()
            self.assertEqual(chk, [("ok",)])
            self.assertGreaterEqual(cnt, 10)

    def test_update_pp_detached_head_and_rollback_with_labels(self):
        import subprocess

        updater = str(ROOT / "update-pp")
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            remote = base / "remote.git"
            work = base / "work"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

            # Seed remote with v0.5 and v0.6 branches (and modify training_labels.json between v0.5 commits!)
            seed = base / "seed"
            subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
            subprocess.run(["git", "checkout", "-b", "v0.5"], cwd=seed, check=True, capture_output=True)
            (seed / ".env.example").write_text("KEEP_FOOTAGE=true\n")
            (seed / "captures" / "review_clips").mkdir(parents=True)
            (seed / "captures" / "review_clips" / "training_labels.json").write_text('[{"clip_id": "seed_v1"}]\n')
            subprocess.run(["git", "add", "."], cwd=seed, check=True)
            subprocess.run(["git", "commit", "-m", "v0.5 initial"], cwd=seed, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "v0.5"], cwd=seed, check=True, capture_output=True)

            # Advance v0.5 on remote AND also change training_labels.json upstream to test conflict handling
            (seed / "v05_patch.txt").write_text("patched on v0.5\n")
            (seed / "captures" / "review_clips" / "training_labels.json").write_text('[{"clip_id": "seed_v2_upstream"}]\n')
            subprocess.run(["git", "add", "."], cwd=seed, check=True)
            subprocess.run(["git", "commit", "-m", "v0.5 patch"], cwd=seed, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "v0.5"], cwd=seed, check=True, capture_output=True)

            subprocess.run(["git", "checkout", "-b", "v0.6"], cwd=seed, check=True, capture_output=True)
            (seed / "v06_feature.txt").write_text("v0.6 feature\n")
            subprocess.run(["git", "add", "v06_feature.txt"], cwd=seed, check=True)
            subprocess.run(["git", "commit", "-m", "v0.6 release"], cwd=seed, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "v0.6"], cwd=seed, check=True, capture_output=True)

            # Clone v0.5 at the first commit into work/
            subprocess.run(["git", "clone", "-b", "v0.5", str(remote), str(work)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"], cwd=work, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
            subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=work, check=True, capture_output=True)
            old_commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=work, text=True).strip()
            (work / ".env").write_text("MOLLY_PASS=keepme\n")
            (work / "data").mkdir(exist_ok=True)
            (work / "captures" / "review_clips").mkdir(parents=True, exist_ok=True)
            custom_labels = '[{"clip_id": "operator_labeled_clip", "user_reviewed": true}]\n'
            (work / "captures" / "review_clips" / "training_labels.json").write_text(custom_labels)

            env = {**os.environ, "POOR_PUPPY_DIR": str(work), "PP_SKIP_DOCKER": "1"}

            # 0) --branch and --latest together must error
            r_both = subprocess.run([updater, "--branch", "v0.5", "--latest"], env=env, capture_output=True, text=True)
            self.assertNotEqual(r_both.returncode, 0)

            # 1) Detached HEAD must ABORT on bare ./update-pp unless --branch or --latest is passed
            subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=work, check=True, capture_output=True)
            r_det = subprocess.run([updater], env=env, capture_output=True, text=True)
            self.assertNotEqual(r_det.returncode, 0)
            self.assertIn("detached HEAD", r_det.stderr)
            # Re-attach to v0.5
            subprocess.run(["git", "checkout", "v0.5"], cwd=work, check=True, capture_output=True)
            (work / "captures" / "review_clips" / "training_labels.json").write_text(custom_labels)

            # 2) Test --rollback-on-fail when healthcheck fails on new commit (with dirty + upstream-changed labels)
            fake_docker = base / "fake_docker.sh"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"$1\" = \"inspect\" ]; then\n"
                f"  cur=$(git -C {str(work)!r} rev-parse --short HEAD)\n"
                f"  if [ \"$cur\" = {old_commit!r} ]; then echo healthy; else echo unhealthy; fi\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            fake_docker.chmod(0o755)
            rb_env = {
                **os.environ,
                "POOR_PUPPY_DIR": str(work),
                "PP_SKIP_DOCKER": "0",
                "PP_DOCKER_BIN": str(fake_docker),
                "PP_HEALTH_RETRIES": "2",
                "PP_HEALTH_SLEEP": "0",
            }
            r_rb = subprocess.run([updater, "--rollback-on-fail"], env=rb_env, capture_output=True, text=True)
            self.assertNotEqual(r_rb.returncode, 0)
            self.assertIn(f"Rollback to {old_commit} succeeded", r_rb.stdout)
            cur_after_rb = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=work, text=True).strip()
            self.assertEqual(cur_after_rb, old_commit)
            self.assertEqual((work / "captures" / "review_clips" / "training_labels.json").read_text(), custom_labels)

            # 3) With --no-rollback (v0.6: rollback is the default), the exact manual rollback command is printed
            subprocess.run(["git", "checkout", "-B", "v0.5", old_commit], cwd=work, check=True, capture_output=True)
            (work / "captures" / "review_clips" / "training_labels.json").write_text(custom_labels)
            r_fail = subprocess.run([updater, "--no-rollback"], env=rb_env, capture_output=True, text=True)
            self.assertNotEqual(r_fail.returncode, 0)
            self.assertIn(f"git checkout -B v0.5 {old_commit} && update-pp --no-pull", r_fail.stderr)
            # Execute the exact manual rollback command under set -e (docker skipped: the fake docker
            # here never prints the daemon start line / selftest reply, so a real --no-pull would roll back)
            subprocess.run(
                ["bash", "-ec", f"git checkout -B v0.5 {old_commit} && {updater} --no-pull"],
                cwd=work, env={**rb_env, "PP_SKIP_DOCKER": "1"}, capture_output=True, text=True, check=True,
            )
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=work, text=True).strip(),
                old_commit,
            )
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work, text=True).strip(),
                "v0.5",  # stays on the branch (no detached HEAD after a rollback)
            )
            self.assertEqual((work / "captures" / "review_clips" / "training_labels.json").read_text(), custom_labels)

            # 4) Re-attach v0.5 and verify normal bare update-pp stays on v0.5, saves .upstream notice, and --latest moves to v0.6
            subprocess.run(["git", "checkout", "-B", "v0.5", old_commit], cwd=work, check=True, capture_output=True)
            (work / "captures" / "review_clips" / "training_labels.json").write_text(custom_labels)
            r1 = subprocess.run([updater], env=env, capture_output=True, text=True, check=True)
            self.assertIn("Branch:     v0.5 -> v0.5", r1.stdout)
            self.assertIn("preserved local labels", r1.stdout)
            self.assertTrue((work / "captures" / "review_clips" / "training_labels.json.upstream").exists())
            self.assertTrue((work / "v05_patch.txt").exists())
            self.assertFalse((work / "v06_feature.txt").exists())
            self.assertEqual((work / "captures" / "review_clips" / "training_labels.json").read_text(), custom_labels)

            r2 = subprocess.run([updater, "--latest"], env=env, capture_output=True, text=True, check=True)
            self.assertIn("Branch:     v0.5 -> v0.6", r2.stdout)
            self.assertTrue((work / "v06_feature.txt").exists())
            self.assertEqual((work / "captures" / "review_clips" / "training_labels.json").read_text(), custom_labels)


if __name__ == "__main__":
    unittest.main()
