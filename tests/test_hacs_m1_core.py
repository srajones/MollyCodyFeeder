"""Unit tests for M1 Core Library, HACS/HA Manifests, Constants, and Standalone HA Compat Layer."""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from custom_components.molly_cody_feeder import _ha_compat as hc
from custom_components.molly_cody_feeder import const
from custom_components.molly_cody_feeder.core import (
    API_URL,
    APP_ID,
    BOWL_CHECK,
    CAMERA_UNAVAILABLE,
    CERT_SHA256,
    DISPENSED,
    DP_NAMES,
    HMAC_KEY,
    MOTOR_NO_KIBBLE,
    MQTT_HOST,
    MQTT_PORT,
    NO_MOTOR,
    PKG_NAME,
    RECORDING_CUT_SHORT,
    SIGNED_KEYS,
    UNVERIFIED,
    AudioAnalysisResult,
    BowlAnalyzer,
    FeedVerifier,
    HouseholdAlerter,
    KibbleDatabase,
    MollyFeederClient,
    RetentionManager,
    RuntimeConfig,
    SpeakerMixin,
    VerifyRequest,
    VisualAnalysisResult,
    WatchdogConfig,
    _alaw_to_pcm16,
    _build_sign_input,
    _decrypt_mqtt_22,
    _encrypt_mqtt_22,
    _get_ch_key,
    _imm_build_ctrl,
    _imm_build_login,
    _imm_build_talk_frame,
    _imm_login_token,
    _imm_mac_sha1,
    _imm_open_aes,
    _imm_seal_aes,
    _motor_active_recently,
    _motor_window,
    _pcm16_to_ulaw,
    _post_data_md5_hex,
    _recording_coverage,
    assess_dispense,
    audio_features,
    classify,
    compute_ha_summary,
    decode_meal_plan,
    decode_molly_schedule,
    decide_passed,
    encode_molly_schedule,
    is_clog,
    is_unverified,
    kibble_flow,
    parse_days_spec,
    recording_cut_short,
    status_label,
    ting_times,
    update_molly_schedule_slot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

LIVE_SCHEDULE = (
    "7f0700201000000"
    "7f1100201000000"
    "7f1600201000000"
    "7f1900171000000"
    "000908100000000"
)


def _write_synthetic_wav(path: Path, motor_sec: float = 3.0, with_tings: bool = True) -> np.ndarray:
    sr = 8000
    total_sec = 1.5 + motor_sec
    t = np.arange(int(sr * total_sec)) / float(sr)
    sig = 0.001 * np.sin(2.0 * np.pi * 100.0 * t)
    if motor_sec > 0:
        m_mask = (t >= 0.5) & (t < 0.5 + motor_sec)
        sig[m_mask] += 0.05 * np.sin(2.0 * np.pi * 190.0 * t[m_mask]) + 0.03 * np.sin(
            2.0 * np.pi * 310.0 * t[m_mask]
        )
    if with_tings and motor_sec > 0.3:
        for onset in np.arange(0.65, 0.5 + motor_sec - 0.15, 0.11):
            i0 = int(round(onset * sr))
            i1 = min(len(sig), i0 + int(0.016 * sr))
            tt = np.arange(i1 - i0) / float(sr)
            sig[i0:i1] += np.hanning(len(tt)) * (
                0.45 * np.sin(2.0 * np.pi * 1120.0 * tt) + 0.45 * np.sin(2.0 * np.pi * 3150.0 * tt)
            )
    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return sig.astype(np.float32)


def _write_bowl_jpg(path: Path, *, is_ir: bool = False, with_kibble: bool = False) -> None:
    h, w = 1280, 720
    if is_ir:
        arr = np.full((h, w, 3), 130, dtype=np.uint8)
        by1, by2 = int(0.88 * h), int(0.995 * h)
        bx1, bx2 = int(0.22 * w), int(0.88 * w)
        arr[by1:by2, bx1:bx2] = 220
        if with_kibble:
            arr[by1 : by1 + int((by2 - by1) * 0.45), bx1:bx2] = 25
    else:
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        arr[:, :] = (200, 150, 95)
        py1, py2 = int(0.88 * h), int(0.985 * h)
        px1, px2 = int(0.32 * w), int(0.75 * w)
        arr[py1:py2, px1:px2] = (175, 165, 150)
        if with_kibble:
            arr[py1 : py1 + int((py2 - py1) * 0.55), px1:bx2 if False else px2] = (1, 1, 1)
    Image.fromarray(arr, mode="RGB").save(str(path), format="JPEG", quality=96)


class TestM1ManifestsAndConstants(unittest.TestCase):
    """Validates hacs.json, manifest.json, and const.py contracts."""

    def test_hacs_json_schema(self):
        hacs_file = REPO_ROOT / "hacs.json"
        self.assertTrue(hacs_file.is_file())
        data = json.loads(hacs_file.read_text(encoding="utf-8"))
        self.assertIn("Molly & Cody Smart Feeder", data["name"])
        self.assertTrue(data["render_readme"])
        self.assertEqual(data["homeassistant"], "2024.4.0")

    def test_manifest_json_schema(self):
        manifest_file = REPO_ROOT / "custom_components" / "molly_cody_feeder" / "manifest.json"
        self.assertTrue(manifest_file.is_file())
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(data["domain"], "molly_cody_feeder")
        self.assertEqual(data["version"], "0.7.0")
        self.assertTrue(data["config_flow"])
        self.assertIn(data["iot_class"], ("cloud_polling", "local_polling"))
        self.assertIsInstance(data["requirements"], list)
        self.assertGreaterEqual(len(data["requirements"]), 5)

    def test_const_exports_platforms_events_and_services(self):
        self.assertEqual(const.DOMAIN, "molly_cody_feeder")
        self.assertEqual(const.VERSION, "0.7.0")
        self.assertEqual(
            set(const.PLATFORMS),
            {"sensor", "binary_sensor", "switch", "button", "number", "select", "camera", "image", "event"},
        )
        self.assertEqual(const.EVENT_FEED, "molly_cody_feeder_feed_event")
        self.assertEqual(const.EVENT_ALERT, "molly_cody_feeder_alert_event")
        self.assertEqual(len(const.ALL_SERVICES), 14)
        for toggle in (
            "CONF_ENABLE_CAMERA",
            "CONF_ENABLE_TTS",
            "CONF_ENABLE_VERIFICATION",
            "CONF_ENABLE_VISION_VERIFICATION",
            "CONF_ENABLE_MP4_RECORDING",
            "CONF_ENABLE_AUTO_RETRY",
        ):
            self.assertTrue(hasattr(const, toggle))

    def test_core_zero_watchdog_imports_and_forbidden_tokens(self):
        core_dir = REPO_ROOT / "custom_components" / "molly_cody_feeder" / "core"
        py_files = list(core_dir.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 20)
        for pf in py_files:
            src = pf.read_text(encoding="utf-8")
            for forbidden in ("vps_install.sh", "review_server.py", "docker-compose"):
                self.assertNotIn(forbidden, src)
            tree = ast.parse(src, filename=str(pf))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name == "watchdog" or alias.name.startswith("watchdog."))
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertFalse(mod == "watchdog" or mod.startswith("watchdog."))


class TestM1HaCompatLayer(unittest.IsolatedAsyncioTestCase):
    """Validates _ha_compat.py standalone classes, coordinator, entities, and flows."""

    async def test_hass_config_entry_services_and_bus(self):
        with tempfile.TemporaryDirectory() as td:
            hass = hc.HomeAssistant(td)
            self.assertTrue(hass.config.path("sub", "file.txt").startswith(td))

            entry = hc.ConfigEntry(
                entry_id="e1",
                domain=const.DOMAIN,
                title="Test Feeder",
                data={"device_id": "dev_1", "password": "secret"},
                options={"scan_interval": 30},
            )
            hass.config_entries.async_update_entry(entry, options={"scan_interval": 45})
            self.assertEqual(entry.options["scan_interval"], 45)

            received = []
            unsub = hass.bus.async_listen(const.EVENT_FEED, lambda ev: received.append(ev.data))
            hass.bus.async_fire(const.EVENT_FEED, {"portions": 2})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["portions"], 2)
            unsub()

            called = {}

            async def _svc_handler(call: hc.ServiceCall):
                called["data"] = call.data
                return {"ok": True}

            schema = hc.vol.Schema({hc.vol.Required("portions"): hc.vol.All(hc.vol.Coerce(int), hc.vol.Range(min=1, max=20))})
            hass.services.async_register(const.DOMAIN, "feed", _svc_handler, schema=schema)
            self.assertTrue(hass.services.has_service(const.DOMAIN, "feed"))
            res = await hass.services.async_call(const.DOMAIN, "feed", {"portions": "4"})
            self.assertEqual(called["data"]["portions"], 4)
            self.assertEqual(res, {"ok": True})

            redacted = hc.async_redact_data({"password": "secret", "nested": {"local_key": "k1", "ok": 1}}, {"password", "local_key"})
            self.assertEqual(redacted["password"], "**REDACTED**")
            self.assertEqual(redacted["nested"]["local_key"], "**REDACTED**")
            self.assertEqual(redacted["nested"]["ok"], 1)

    async def test_coordinator_and_all_entity_base_classes(self):
        import logging

        with tempfile.TemporaryDirectory() as td:
            hass = hc.HomeAssistant(td)
            coord = hc.DataUpdateCoordinator(
                hass,
                logging.getLogger("test"),
                name="test_coord",
                update_method=lambda: self._dummy_update(),
            )
            await coord.async_config_entry_first_refresh()
            self.assertEqual(coord.data, {"status": "ok"})

            ce = hc.CoordinatorEntity(coord)
            self.assertTrue(ce.available)

            ev_ent = hc.EventEntity()
            ev_ent._attr_event_types = ["dispensed"]
            ev_ent._trigger_event("dispensed", {"portions": 3})
            self.assertEqual(ev_ent.extra_state_attributes["event_type"], "dispensed")
            self.assertEqual(ev_ent.extra_state_attributes["portions"], 3)

            img_ent = hc.ImageEntity(hass)
            self.assertEqual(img_ent.content_type, "image/jpeg")
            self.assertIsNone(await img_ent.async_image())

    @staticmethod
    async def _dummy_update():
        return {"status": "ok"}


class TestM1CoreLibrary(unittest.TestCase):
    """Validates core crypto, schedule codec, audio/visual analysis, decision, DB, and retention."""

    def test_crypto_and_schedule_codec(self):
        self.assertEqual(APP_ID, "wr7phe79kh4ntutc8g43")
        self.assertEqual(PKG_NAME, "and.mollycody.app01")
        self.assertEqual(API_URL, "https://a1.tuyaus.com/api.json")
        self.assertEqual(MQTT_HOST, "m1-us.mollycody.tech")
        self.assertEqual(MQTT_PORT, 8883)
        self.assertIn(PKG_NAME, HMAC_KEY)
        self.assertIn("postData", SIGNED_KEYS)
        self.assertEqual(DP_NAMES["201"], "feed_num")

        ch_key = _get_ch_key()
        expected_ch = hmac.new(
            APP_ID.encode("utf-8"), f"{PKG_NAME}_{CERT_SHA256}".encode("utf-8"), hashlib.sha256
        ).hexdigest().lower()[8:16]
        self.assertEqual(ch_key, expected_ch)

        pkt = _encrypt_mqtt_22('{"dps":{"201":2}}', "0123456789abcdef", seq=5, source=1)
        self.assertEqual(_decrypt_mqtt_22(pkt, "0123456789abcdef"), {"dps": {"201": 2}})

        meals = decode_molly_schedule(LIVE_SCHEDULE)
        self.assertEqual(len(meals), 5)
        self.assertEqual(encode_molly_schedule(meals), LIVE_SCHEDULE)
        updated = update_molly_schedule_slot(LIVE_SCHEDULE, slot=1, hour=6, minute=30, portions=5, enabled=True)
        self.assertEqual(decode_molly_schedule(updated)[0].time_str, "06:30")
        self.assertEqual(parse_days_spec("weekdays"), 0x1F)
        self.assertEqual(len(decode_meal_plan(LIVE_SCHEDULE)), 5)

    def test_imm_codec_and_speaker_framing(self):
        tok = _imm_login_token("pwd", "key")
        login_pkt = _imm_build_login(tok)
        self.assertEqual(len(login_pkt), 104)

        aes_key = b"0123456789abcdef"
        sealed = _imm_seal_aes(aes_key, b"hello_imm")
        self.assertEqual(_imm_open_aes(aes_key, sealed), b"hello_imm")
        self.assertEqual(len(_imm_mac_sha1(aes_key, b"seg")), 20)

        pcm = _alaw_to_pcm16(bytes([0xD5, 0x55]))
        self.assertEqual(len(pcm), 2)
        ulaw = _pcm16_to_ulaw(np.zeros(160, dtype=np.int16))
        self.assertEqual(len(ulaw), 160)

        ctrl = _imm_build_ctrl(SpeakerMixin.TALK_REQ_ID, SpeakerMixin.TALK_OPEN_CMD, SpeakerMixin.TALK_OPEN_PAYLOAD)
        self.assertEqual(struct.unpack_from("<IIIII", ctrl, 0)[1], 7)
        talk = _imm_build_talk_frame(1, 320, 1000, b"\xff" * 320, req_id=7)
        self.assertEqual(len(talk), 352)

        chime = SpeakerMixin._build_chime_alaw(8000)
        self.assertGreaterEqual(len(chime), 3200)

        chunks = [np.zeros(320, dtype=np.int16)] * 25 + [np.full(320, 2500, dtype=np.int16)] * 35
        rx_t = [10.0 + (i + 1) * 0.04 for i in range(len(chunks))]
        self.assertTrue(_motor_active_recently(chunks))
        self.assertIsNotNone(_motor_window(chunks, rx_t))
        cov = _recording_coverage(chunks, 8000, rx_t, 10.0, 12.4)
        self.assertEqual(cov["audio_sec"], 2.4)

    def test_acoustic_and_visual_analyzer_and_decision(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            hass = hc.HomeAssistant(td)
            entry = hc.ConfigEntry(
                entry_id="entry_m1",
                data={"device_id": "dev_m1", "connection_mode": "auto"},
                options={"hopper_capacity_portions": 220, "connection_mode": "lan"},
            )
            rt = RuntimeConfig.from_entry(hass, entry)
            self.assertEqual(rt.connection_mode, "lan")
            self.assertEqual(rt.hopper_capacity_portions, 220)
            self.assertIn("entry_m1", str(rt.db_path))

            analyzer = BowlAnalyzer(rt)
            wav_ok = td_p / "ok.wav"
            samples = _write_synthetic_wav(wav_ok, motor_sec=3.0, with_tings=True)
            self.assertGreaterEqual(len(ting_times(samples, 8000)), 15)
            flow = kibble_flow(samples, 8000, 0.5, 3.5)
            self.assertIsNotNone(flow)

            res_ok = analyzer.analyze_dispense_audio(str(wav_ok))
            self.assertTrue(res_ok.motor_detected)
            self.assertTrue(res_ok.kibble_dispensed_audio)
            self.assertEqual(len(audio_features(res_ok)), 5)
            flags = assess_dispense(res_ok, portions=1)
            self.assertFalse(flags["weak_flow"])

            wav_clog = td_p / "clog.wav"
            _write_synthetic_wav(wav_clog, motor_sec=3.0, with_tings=False)
            res_clog = analyzer.analyze_dispense_audio(str(wav_clog))
            self.assertTrue(res_clog.motor_detected)
            self.assertFalse(res_clog.kibble_dispensed_audio)

            bef_jpg = td_p / "before.jpg"
            aft_jpg = td_p / "after.jpg"
            ann_jpg = td_p / "ann.jpg"
            _write_bowl_jpg(bef_jpg, is_ir=False, with_kibble=False)
            _write_bowl_jpg(aft_jpg, is_ir=False, with_kibble=True)
            vis_res = analyzer.analyze_bowl_visual(str(aft_jpg), str(bef_jpg), str(ann_jpg))
            self.assertFalse(vis_res.bowl_empty)
            self.assertTrue(vis_res.food_increased)
            self.assertTrue(ann_jpg.exists())

            ir_bef = td_p / "ir_bef.jpg"
            ir_aft = td_p / "ir_aft.jpg"
            _write_bowl_jpg(ir_bef, is_ir=True, with_kibble=False)
            _write_bowl_jpg(ir_aft, is_ir=True, with_kibble=True)
            vis_ir = analyzer.analyze_bowl_visual(str(ir_aft), str(ir_bef))
            self.assertTrue(vis_ir.is_infrared_mode)
            self.assertTrue(vis_ir.food_increased)

            self.assertTrue(decide_passed(res_ok, vis_res, 1, True))
            self.assertFalse(decide_passed(res_clog, vis_res, 1, True))
            self.assertEqual(classify(res_ok, vis_res, True, True), (DISPENSED, True))
            self.assertEqual(classify(res_clog, vis_res, True, False), (MOTOR_NO_KIBBLE, False))
            self.assertEqual(classify(res_clog, vis_res, True, False, cut_short=True), (RECORDING_CUT_SHORT, False))
            self.assertTrue(is_clog(MOTOR_NO_KIBBLE, False))
            self.assertFalse(is_clog(RECORDING_CUT_SHORT, False))
            self.assertTrue(is_unverified(RECORDING_CUT_SHORT))
            self.assertTrue(is_unverified(CAMERA_UNAVAILABLE))
            self.assertEqual(status_label(DISPENSED, True), "ok")
            self.assertTrue(recording_cut_short({"recorded_wall_sec": 12.0, "audio_sec": 2.0, "audio_last_rx_sec": 2.0}))

    def test_database_verifier_summary_and_retention(self):
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            cfg = WatchdogConfig(
                storage_dir=td_p,
                keep_footage=True,
                capture_retention_days=7.0,
                export_ml_on_prune=True,
                eaten_check_minutes=0,
            )
            db = KibbleDatabase(cfg.db_path)
            db.record_refill(200, "Initial fill")

            molly = mock.MagicMock(mode="cloud")
            molly.get_status.return_value = {
                "food_weight_g": 15,
                "feed_abnormal": 1,
                "dps": {"207": LIVE_SCHEDULE, "255": 1},
                "active_transport": "cloud",
            }
            molly.dispense_food.return_value = (True, "cloud")

            wav_ok = td_p / "seed_ok.wav"
            _write_synthetic_wav(wav_ok, motor_sec=3.0, with_tings=True)
            bef_seed = td_p / "seed_bef.jpg"
            aft_seed = td_p / "seed_aft.jpg"
            _write_bowl_jpg(bef_seed, is_ir=False, with_kibble=False)
            _write_bowl_jpg(aft_seed, is_ir=False, with_kibble=True)

            def _fake_capture(**kw):
                if kw.get("trigger_fn"):
                    kw["trigger_fn"]()
                if kw.get("output_wav"):
                    Path(kw["output_wav"]).write_bytes(wav_ok.read_bytes())
                if kw.get("before_jpg"):
                    Path(kw["before_jpg"]).write_bytes(bef_seed.read_bytes())
                if kw.get("after_jpg"):
                    Path(kw["after_jpg"]).write_bytes(aft_seed.read_bytes())
                return {
                    "audio_ok": True,
                    "before_ok": True,
                    "after_ok": True,
                    "recorded_wall_sec": 6.0,
                    "audio_sec": 6.0,
                    "audio_last_rx_sec": 5.95,
                }

            molly.capture_av_clip.side_effect = _fake_capture
            analyzer = BowlAnalyzer(cfg)
            alerter = HouseholdAlerter(cfg)
            latest_snap = td_p / "latest.jpg"

            verifier = FeedVerifier(cfg, molly, analyzer, db, alerter, cfg.captures_dir, latest_snap)
            passed, state = verifier.run(VerifyRequest(trigger_dispense=True, portions=4))
            self.assertTrue(passed)
            self.assertEqual(state["outcome"], DISPENSED)

            hop = db.hopper_status(200)
            self.assertEqual(hop["portions_used"], 4)
            self.assertEqual(hop["portions_left"], 196)

            slots = [m.to_dict() for m in decode_molly_schedule(LIVE_SCHEDULE)]
            summary = compute_ha_summary(db, slots, state, hopper_capacity=200)
            self.assertEqual(summary["today_portions"], 4)
            self.assertEqual(summary["hopper_portions_left"], 196)

            rm = RetentionManager(cfg, db, cfg.captures_dir, protected=[latest_snap])
            self.assertIsInstance(rm.status(), dict)

    # ------------------------------------------------------------------
    # 7. Audio-only _ImmSessionProtocol skips cmd 6 (video) and sends 0x00040006
    # ------------------------------------------------------------------
    def test_imm_session_protocol_audio_only_skips_video_cmd(self) -> None:
        from custom_components.molly_cody_feeder.core.molly.codec import (
            _IMM_CMD_START_AUDIO,
            _IMM_CMD_START_VIDEO,
        )
        from custom_components.molly_cody_feeder.core.molly.imm_transport import (
            _ImmSessionProtocol,
        )

        proto_audio = _ImmSessionProtocol("ufrag", "pwd", decode_video=False, audio_only=True)
        proto_audio.aes_key = b"0123456789abcdef"
        self.assertTrue(proto_audio.audio_only)
        self.assertFalse(proto_audio.decode_video)
        self.assertIsNone(proto_audio.vcodec)

        with mock.patch.object(proto_audio, "send_kcp_msg") as m_send:
            msgs = proto_audio._on_login_success()
            self.assertEqual(m_send.call_count, len(msgs))
            self.assertIn(_IMM_CMD_START_AUDIO, proto_audio.sent_ctrl_cmds)
            self.assertIn(5, proto_audio.sent_ctrl_cmds)
            self.assertNotIn(_IMM_CMD_START_VIDEO, proto_audio.sent_ctrl_cmds)
            self.assertNotIn(9, proto_audio.sent_ctrl_cmds)

        proto_av = _ImmSessionProtocol("ufrag", "pwd", decode_video=False, audio_only=False)
        proto_av.aes_key = b"0123456789abcdef"
        with mock.patch.object(proto_av, "send_kcp_msg"):
            proto_av._on_login_success()
            self.assertIn(_IMM_CMD_START_VIDEO, proto_av.sent_ctrl_cmds)
            self.assertIn(_IMM_CMD_START_AUDIO, proto_av.sent_ctrl_cmds)

    # ------------------------------------------------------------------
    # 8. Pure-Python WAV writer & capture_audio_clip without ffmpeg
    # ------------------------------------------------------------------
    def test_capture_audio_clip_writes_pure_python_wav_without_ffmpeg(self) -> None:
        from custom_components.molly_cody_feeder.core.molly.codec import (
            _ALAW_TABLE,
            alaw_to_pcm16_bytes,
            write_wav_pure_python,
        )
        from custom_components.molly_cody_feeder.core.molly.media import (
            MediaEncodingMixin,
        )

        self.assertEqual(len(_ALAW_TABLE), 256)
        raw_alaw = bytes([0xD5, 0x55, 0x80, 0x00] * 400)
        pcm_bytes = alaw_to_pcm16_bytes(raw_alaw)
        self.assertEqual(len(pcm_bytes), len(raw_alaw) * 2)

        with tempfile.TemporaryDirectory() as td:
            wav_p1 = Path(td) / "pure_alaw.wav"
            wav_p2 = Path(td) / "pure_pcm.wav"
            with mock.patch("subprocess.run", side_effect=AssertionError("ffmpeg must never be called")):
                self.assertTrue(write_wav_pure_python(str(wav_p1), [raw_alaw], sample_rate=8000, is_alaw=True))
                self.assertTrue(
                    MediaEncodingMixin._write_audio_wav(
                        [_alaw_to_pcm16(raw_alaw)], 8000, str(wav_p2)
                    )
                )

            with wave.open(str(wav_p1), "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 8000)
                self.assertEqual(wf.getnframes(), len(raw_alaw))

            client = MollyFeederClient.__new__(MollyFeederClient)
            client.mode = "cloud"
            clip_out = Path(td) / "captured_audio.wav"
            triggered = {"count": 0}

            def _fake_imm_session(**kwargs):
                self.assertTrue(kwargs.get("audio_only"))
                self.assertFalse(kwargs.get("decode_video"))
                self.assertEqual(kwargs.get("output_mp4"), "")
                if kwargs.get("trigger_fn"):
                    kwargs["trigger_fn"]()
                write_wav_pure_python(str(kwargs["output_wav"]), [raw_alaw] * 5, sample_rate=8000, is_alaw=True)
                return {
                    "video_ok": False,
                    "audio_ok": True,
                    "wav_saved": True,
                    "mp4_saved": False,
                    "before_ok": False,
                    "after_ok": False,
                    "video_frames": 0,
                    "audio_frames": 5,
                    "audio_bytes": len(raw_alaw) * 10,
                    "audio_rate": 8000,
                    "audio_only": True,
                    "motor_duration_sec": 2.7,
                    "motor_window_sec": [1.0, 3.7],
                    "recorded_wall_sec": 6.0,
                    "audio_sec": 6.0,
                    "audio_last_rx_sec": 5.95,
                }

            def _run_fake_coro(coro):
                f_locals = dict(coro.cr_frame.f_locals) if getattr(coro, "cr_frame", None) else {}
                coro.close()
                return _fake_imm_session(**f_locals)

            with mock.patch("subprocess.run", side_effect=AssertionError("ffmpeg must never be called")):
                with mock.patch.object(client, "_run_session", side_effect=_run_fake_coro):
                    res = client.capture_audio_clip(
                        output_wav_path=str(clip_out),
                        duration_sec=6.0,
                        trigger_fn=lambda: (triggered.__setitem__("count", triggered["count"] + 1), (True, "cloud"))[1],
                        trigger_delay_sec=1.0,
                    )
            self.assertEqual(triggered["count"], 1)
            self.assertTrue(res["wav_saved"])
            self.assertTrue(res["audio_ok"])
            self.assertTrue(res["audio_only"])
            self.assertTrue(res["trigger_ok"])
            self.assertEqual(res["trigger_detail"], "cloud")
            self.assertGreater(res["audio_bytes"], 0)
            self.assertAlmostEqual(res["motor_duration_sec"], 2.7, places=1)
            self.assertTrue(clip_out.is_file())

    # ------------------------------------------------------------------
    # 9. Allocation-frugal (< 2 MB) NumPy & stdlib fallback classifier on fixtures
    # ------------------------------------------------------------------
    def test_audio_only_classifier_accuracy_and_memory_footprint(self) -> None:
        import tracemalloc
        from custom_components.molly_cody_feeder.core.analysis import (
            _analyze_audio_stdlib,
            _analyze_flow_stdlib,
            _analyze_tings_stdlib,
        )
        from custom_components.molly_cody_feeder.core.analysis import audio as audio_mod

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            analyzer = BowlAnalyzer(RuntimeConfig(captures_dir=td_p))

            wav_ok = td_p / "dispense_ok.wav"
            wav_clog = td_p / "dispense_clog.wav"
            wav_silent = td_p / "dispense_silent.wav"
            s_ok = _write_synthetic_wav(wav_ok, motor_sec=3.0, with_tings=True)
            s_clog = _write_synthetic_wav(wav_clog, motor_sec=3.0, with_tings=False)
            _write_synthetic_wav(wav_silent, motor_sec=0.0, with_tings=False)

            # Check verify_audio_only helper
            v_ok = analyzer.verify_audio_only(str(wav_ok), portions=1)
            self.assertTrue(v_ok["passed"])
            self.assertEqual(v_ok["outcome"], "dispensed")
            self.assertFalse(v_ok["clog_detected"])

            v_clog = analyzer.verify_audio_only(str(wav_clog), portions=1)
            self.assertFalse(v_clog["passed"])
            self.assertEqual(v_clog["outcome"], "motor_no_kibble")
            self.assertTrue(v_clog["clog_detected"])

            v_nomotor = analyzer.verify_audio_only(str(wav_silent), portions=1)
            self.assertFalse(v_nomotor["passed"])
            self.assertEqual(v_nomotor["outcome"], "no_motor")
            self.assertTrue(v_nomotor["clog_detected"])

            # Pure-Python stdlib functions directly (including < 2 MB peak RAM check)
            tracemalloc.start()
            self.assertGreaterEqual(len(_analyze_tings_stdlib(s_ok, 8000)), 15)
            self.assertEqual(len(_analyze_tings_stdlib(s_clog, 8000)), 0)
            flow_std = _analyze_flow_stdlib(s_ok, 8000, 1.0, 4.0)
            self.assertIsNotNone(flow_std)
            assert flow_std is not None
            self.assertGreater(flow_std.coverage, 0.5)
            r_std_ok = _analyze_audio_stdlib(str(wav_ok), analyzer.cfg)
            _, peak_std_synth = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(peak_std_synth, 2 * 1024 * 1024)
            self.assertTrue(r_std_ok.motor_detected)
            self.assertTrue(r_std_ok.kibble_dispensed_audio)

            r_std_clog = _analyze_audio_stdlib(str(wav_clog), analyzer.cfg)
            self.assertTrue(r_std_clog.motor_detected)
            self.assertFalse(r_std_clog.kibble_dispensed_audio)

            r_std_silent = _analyze_audio_stdlib(str(wav_silent), analyzer.cfg)
            self.assertFalse(r_std_silent.motor_detected)
            self.assertFalse(r_std_silent.kibble_dispensed_audio)

            # Ground-truth WAV fixtures in tests/fixtures/labeled_clips/
            clips_dir = REPO_ROOT / "tests" / "fixtures" / "labeled_clips"
            labels_file = clips_dir / "training_labels.json"
            if labels_file.is_file():
                labels = json.loads(labels_file.read_text(encoding="utf-8"))
                for rec in labels:
                    if not rec.get("user_reviewed"):
                        continue
                    wav_f = clips_dir / f"{rec['clip_id']}.wav"
                    if not wav_f.is_file():
                        continue
                    expected_ok = bool(rec["feed_success"])

                    # 1. NumPy path (< 2 MB peak memory)
                    tracemalloc.start()
                    res_np = analyzer.analyze_dispense_audio(str(wav_f))
                    _, peak_np = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    self.assertTrue(res_np.motor_detected, f"NumPy motor_detected on {rec['clip_id']}")
                    self.assertEqual(res_np.kibble_dispensed_audio, expected_ok, f"NumPy verdict on {rec['clip_id']}")
                    self.assertLess(peak_np, 2 * 1024 * 1024, f"NumPy peak RAM >= 2MB on {rec['clip_id']}: {peak_np}")

                    # 2. Pure-Python stdlib fallback path (HAS_NUMPY=False)
                    with mock.patch.object(audio_mod, "HAS_NUMPY", False):
                        res_py = analyzer.analyze_dispense_audio(str(wav_f))
                    self.assertTrue(res_py.motor_detected, f"Stdlib motor_detected on {rec['clip_id']}")
                    self.assertEqual(res_py.kibble_dispensed_audio, expected_ok, f"Stdlib verdict on {rec['clip_id']}")

    # ------------------------------------------------------------------
    # 10. FeedVerifier in audio-only mode skips vision and passes audio_only=True
    # ------------------------------------------------------------------
    def test_feed_verifier_audio_only_mode_skips_vision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            cfg = RuntimeConfig(
                db_path=td_p / "kibble.db",
                captures_dir=td_p / "captures",
                eaten_check_minutes=0,
                enable_vision_verification=False,
                enable_mp4_recording=False,
            )
            db = KibbleDatabase(str(cfg.db_path))
            molly = mock.MagicMock(mode="cloud")
            molly.get_status.return_value = {
                "food_weight_g": 10,
                "feed_abnormal": 1,
                "dps": {},
                "active_transport": "cloud",
            }
            molly.dispense_food.return_value = (True, "cloud")

            wav_clog = td_p / "seed_clog.wav"
            _write_synthetic_wav(wav_clog, motor_sec=3.0, with_tings=False)
            captured_kwargs: Dict[str, Any] = {}

            def _fake_audio_only_capture(**kw):
                captured_kwargs.update(kw)
                if kw.get("trigger_fn"):
                    kw["trigger_fn"]()
                if kw.get("output_wav"):
                    Path(kw["output_wav"]).write_bytes(wav_clog.read_bytes())
                return {
                    "video_ok": False,
                    "audio_ok": True,
                    "wav_saved": True,
                    "before_ok": False,
                    "after_ok": False,
                    "audio_only": True,
                    "recorded_wall_sec": 6.0,
                    "audio_sec": 6.0,
                    "audio_last_rx_sec": 5.95,
                }

            molly.capture_av_clip.side_effect = _fake_audio_only_capture
            analyzer = BowlAnalyzer(cfg)
            analyzer.analyze_bowl_visual = mock.MagicMock(
                side_effect=AssertionError("analyze_bowl_visual must not be called in audio-only mode")
            )
            alerter = mock.MagicMock()
            verifier = FeedVerifier(cfg, molly, analyzer, db, alerter, cfg.captures_dir, td_p / "latest.jpg")
            passed, state = verifier.run(VerifyRequest(trigger_dispense=True, portions=2))

            self.assertFalse(passed)
            self.assertEqual(state["outcome"], MOTOR_NO_KIBBLE)
            self.assertTrue(state["clog_detected"])
            self.assertTrue(captured_kwargs.get("audio_only"))
            self.assertFalse(captured_kwargs.get("decode_video", True))
            self.assertEqual(captured_kwargs.get("output_mp4"), "")
            self.assertIsNone(captured_kwargs.get("before_jpg"))
            self.assertIsNone(captured_kwargs.get("after_jpg"))
            analyzer.analyze_bowl_visual.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestM4AdversarialCoreChallenges(unittest.TestCase):
    """M4 adversarial stress tests & edge-case challenges for core Molly/Audio/Vision/Decision/DB/Retention."""

    def test_adv_01_camera_module_imports_struct_for_imm_session_startup(self) -> None:
        from custom_components.molly_cody_feeder.core.molly import camera as camera_mod

        self.assertTrue(
            hasattr(camera_mod, "struct"),
            "custom_components.molly_cody_feeder.core.molly.camera is missing `import struct` "
            "required by CameraMixin._imm_session_async line 510 (`struct.unpack_from('<I', raw, 12)[0]`)",
        )

    def test_adv_02_imm_session_protocol_kcp_fragmentation_and_hmac_tamper(self) -> None:
        from custom_components.molly_cody_feeder.core.molly.imm_codec import (
            _IMM_MEDIA_MAGIC,
        )
        from custom_components.molly_cody_feeder.core.molly.imm_transport import (
            _ImmSessionProtocol,
        )

        proto = _ImmSessionProtocol("ufrag", "pwd", decode_video=False, cloud_only=True, audio_only=True)
        proto.aes_key = b"0123456789abcdef"
        sent_packets = []
        transport_mock = mock.MagicMock()
        transport_mock.sendto.side_effect = lambda pkt, addr: sent_packets.append((pkt, addr))
        proto.connection_made(transport_mock)

        # 1. LAN IP packets rejected when cloud_only=True
        ctrl_plain = struct.pack("<IIIII", _IMM_MEDIA_MAGIC, 7, 0, 8, 4) + b"\x00\x00\x00\x00"
        cipher_msg = _imm_seal_aes(proto.aes_key, ctrl_plain)
        seg_hdr = struct.pack("<IBBHIIII", 0, 81, 0, 512, 1000, 0, 0, len(cipher_msg))
        seg = seg_hdr + cipher_msg
        valid_pkt = seg + _imm_mac_sha1(proto.aes_key, seg)

        proto.datagram_received(valid_pkt, ("192.168.1.50", 12345))
        self.assertFalse(proto.ice_connected.is_set())
        self.assertEqual(len(proto.ctrl_replies), 0)

        # 2. Tampered HMAC-SHA1 tag rejected
        bad_pkt = valid_pkt[:-1] + bytes([valid_pkt[-1] ^ 0xFF])
        proto.datagram_received(bad_pkt, ("34.200.10.20", 3478))
        self.assertFalse(proto.ice_connected.is_set())
        self.assertEqual(len(proto.ctrl_replies), 0)

        # 3. Out-of-order KCP fragments (frg=1, frg=0) reassembled in order
        part0 = cipher_msg[:16]
        part1 = cipher_msg[16:]
        s0 = struct.pack("<IBBHIIII", 0, 81, 1, 512, 1001, 0, 0, len(part0)) + part0
        s1 = struct.pack("<IBBHIIII", 0, 81, 0, 512, 1002, 1, 0, len(part1)) + part1
        pkt_s1 = s1 + _imm_mac_sha1(proto.aes_key, s1)
        pkt_s0 = s0 + _imm_mac_sha1(proto.aes_key, s0)

        # Arrive out of order: sn=1 first, then sn=0
        proto.datagram_received(pkt_s1, ("34.200.10.20", 3478))
        self.assertTrue(proto.ice_connected.is_set())
        self.assertFalse(proto.speaker_opened.is_set())
        proto.datagram_received(pkt_s0, ("34.200.10.20", 3478))
        self.assertTrue(proto.speaker_opened.is_set())
        self.assertEqual(len(proto.ctrl_replies), 1)
        self.assertEqual(proto.ctrl_replies[0][2], 8)

    def test_adv_03_never_sent_and_retry_if_unreached_adversarial_matrix(self) -> None:
        import socket
        from custom_components.molly_cody_feeder.core import (
            _never_sent,
            _retry_if_unreached,
        )
        import requests

        self.assertTrue(_never_sent(requests.exceptions.ConnectTimeout("SYN timeout")))
        self.assertTrue(_never_sent(socket.gaierror(-2, "Name or service not known")))
        self.assertTrue(_never_sent(requests.exceptions.ConnectionError("NewConnectionError: refused")))
        self.assertTrue(_never_sent(requests.exceptions.ConnectionError("Failed to establish a new connection")))
        self.assertFalse(_never_sent(requests.exceptions.ReadTimeout("Read timed out")))
        self.assertFalse(_never_sent(requests.exceptions.ConnectionError("Connection reset by peer after write")))
        self.assertFalse(_never_sent(requests.exceptions.HTTPError("HTTP 500")))

        calls = {"n": 0}

        def _flaky_connect() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectTimeout("connect timeout")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(_retry_if_unreached(_flaky_connect, attempts=3, base_delay=0.01), "ok")
        self.assertEqual(calls["n"], 3)

        calls["n"] = 0

        def _read_timeout() -> str:
            calls["n"] += 1
            raise requests.exceptions.ReadTimeout("read timeout")

        with mock.patch("time.sleep"):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                _retry_if_unreached(_read_timeout, attempts=3, base_delay=0.01)
        self.assertEqual(calls["n"], 1)

    def test_adv_04_schedule_codec_adversarial_fuzz_and_slot_boundaries(self) -> None:
        for bad in (
            "",
            "   ",
            "7f070020100000",  # 14 chars
            "7f07002010000001",  # 16 chars
            "GG0700201000000",  # invalid hex mask
            "7f2400201000000",  # hour 24
            "7f0760201000000",  # minute 60
            "7f-100201000000",  # negative hour
            "7f0700xx1000000",  # non-decimal portions
        ):
            self.assertEqual(decode_molly_schedule(bad), [], f"Should reject malformed schedule: {bad!r}")

        for invalid_slot in (0, -1, 9, 99):
            with self.assertRaises(ValueError):
                update_molly_schedule_slot(LIVE_SCHEDULE, slot=invalid_slot, hour=8)

        s8 = update_molly_schedule_slot("", slot=8, hour=99, minute=-5, portions=99, days_mask=0xFF, enabled=True)
        decoded8 = decode_molly_schedule(s8)
        self.assertEqual(len(decoded8), 8)
        self.assertEqual(decoded8[7].hour, 23)
        self.assertEqual(decoded8[7].minute, 0)
        self.assertEqual(decoded8[7].portions, 20)
        self.assertEqual(decoded8[7].days_mask, 0x7F)
        self.assertTrue(decoded8[7].enabled)
        self.assertFalse(decoded8[0].enabled)

        self.assertEqual(parse_days_spec("0x15"), 0x15)
        self.assertEqual(parse_days_spec("once"), 0x00)
        self.assertEqual(parse_days_spec("weekends"), 0x60)
        self.assertEqual(parse_days_spec("mon, wed, fri"), (1 << 0) | (1 << 2) | (1 << 4))
        self.assertEqual(parse_days_spec("invalid_day_name"), 0x7F)

    def test_adv_05_pure_python_aes128_and_rsa_parity_without_cryptography(self) -> None:
        from custom_components.molly_cody_feeder.core.molly import crypto as crypto_mod

        key = b"0123456789abcdef"
        iv = b"fedcba9876543210"
        payload = b"MollyCodySmartFeederTestBlock123"  # 32 bytes

        ecb_c = crypto_mod._aes128_ecb_encrypt(key, payload)
        cbc_c = crypto_mod._aes128_cbc_encrypt(key, iv, payload)
        with mock.patch.object(crypto_mod, "_HAS_CRYPTOGRAPHY", False):
            ecb_py = crypto_mod._aes128_ecb_encrypt(key, payload)
            cbc_py = crypto_mod._aes128_cbc_encrypt(key, iv, payload)
            self.assertEqual(ecb_py, ecb_c)
            self.assertEqual(cbc_py, cbc_c)
            self.assertEqual(crypto_mod._aes128_ecb_decrypt(key, ecb_py), payload)
            self.assertEqual(crypto_mod._aes128_cbc_decrypt(key, iv, cbc_py), payload)

            pkt = _encrypt_mqtt_22('{"dps":{"201":3,"255":1}}', "0123456789abcdef", seq=42, source=1)
            self.assertEqual(_decrypt_mqtt_22(pkt, "0123456789abcdef"), {"dps": {"201": 3, "255": 1}})

    def test_adv_06_g711_alaw_ulaw_and_write_wav_pure_python_all_chunk_types(self) -> None:
        import array
        from custom_components.molly_cody_feeder.core.molly import imm_codec
        from custom_components.molly_cody_feeder.core.molly.imm_codec import write_wav_pure_python

        all_bytes = bytes(range(256))
        with mock.patch.object(imm_codec, "HAS_NUMPY", True):
            pcm_np = imm_codec._alaw_to_pcm16(all_bytes)
            ulaw_np = imm_codec._pcm16_to_ulaw(pcm_np)
        with mock.patch.object(imm_codec, "HAS_NUMPY", False):
            pcm_py = imm_codec._alaw_to_pcm16(all_bytes)
            ulaw_py = imm_codec._pcm16_to_ulaw(pcm_py)
        self.assertEqual(list(pcm_np), list(pcm_py))
        self.assertEqual(ulaw_np, ulaw_py)

        with tempfile.TemporaryDirectory() as td:
            out_wav = Path(td) / "mixed.wav"
            self.assertFalse(write_wav_pure_python("", [b"\x00\x00"]))
            self.assertFalse(write_wav_pure_python(str(out_wav), [None, b"", []]))

            chunks = [
                None,
                b"\x00\x01\x00\x02",
                bytearray(b"\x00\x03\x00\x04"),
                memoryview(b"\x00\x05\x00\x06"),
                np.array([100, -100], dtype=np.int16),
                array.array("h", [200, -200]),
                array.array("i", [300, -300]),
                [400, -400],
            ]
            self.assertTrue(write_wav_pure_python(str(out_wav), chunks, sample_rate=8000, is_alaw=False))
            with wave.open(str(out_wav), "rb") as wf:
                self.assertEqual(wf.getnframes(), 14)

    def test_adv_07_analyze_audio_8bit_stereo_and_unsupported_sampwidth(self) -> None:
        from custom_components.molly_cody_feeder.core.analysis import _analyze_audio_stdlib

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            analyzer = BowlAnalyzer(RuntimeConfig(captures_dir=td_p))

            # 1. 8-bit unsigned stereo WAV (> 1s)
            wav_u8 = td_p / "u8_stereo.wav"
            sr = 8000
            n_frames = sr * 2
            u8_samples = bytes([128] * (n_frames * 2))
            with wave.open(str(wav_u8), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(1)
                wf.setframerate(sr)
                wf.writeframes(u8_samples)

            res_np = analyzer.analyze_dispense_audio(str(wav_u8))
            self.assertFalse(res_np.motor_detected)
            res_std = _analyze_audio_stdlib(str(wav_u8), analyzer.cfg)
            self.assertFalse(res_std.motor_detected)

            # 2. 24-bit (sampwidth=3) WAV raises ValueError in both NumPy and stdlib paths
            wav_24 = td_p / "s24.wav"
            with wave.open(str(wav_24), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(3)
                wf.setframerate(sr)
                wf.writeframes(b"\x00\x00\x00" * sr)

            with self.assertRaises(ValueError):
                analyzer.analyze_dispense_audio(str(wav_24))
            with self.assertRaises(ValueError):
                _analyze_audio_stdlib(str(wav_24), analyzer.cfg)

            # 3. Non-WAV file without ffmpeg raises RuntimeError
            fake_mp4 = td_p / "clip.mp4"
            fake_mp4.write_bytes(b"0000")
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(RuntimeError):
                    analyzer.analyze_dispense_audio(str(fake_mp4))

    def test_adv_08_ting_and_flow_degenerate_signals_zero_nan_inf(self) -> None:
        import math
        from custom_components.molly_cody_feeder.core.analysis import (
            _analyze_flow_stdlib,
            _analyze_tings_stdlib,
        )

        sr = 8000
        for arr in (
            np.zeros(sr * 3, dtype=np.int16),  # all zeros
            np.full(sr * 3, 12000, dtype=np.int16),  # constant DC offset
            np.array([32767, -32768] * (sr + 500), dtype=np.int16),  # Nyquist square wave
            np.zeros(100, dtype=np.int16),  # shorter than FFT window
        ):
            t_np = ting_times(arr, sr)
            t_py = _analyze_tings_stdlib(arr, sr)
            self.assertIsInstance(t_np, list)
            self.assertIsInstance(t_py, list)
            f_np = kibble_flow(arr, sr, 0.2, 2.5)
            f_py = _analyze_flow_stdlib(arr, sr, 0.2, 2.5)
            if f_np is not None:
                self.assertFalse(math.isnan(f_np.coverage) or math.isinf(f_np.coverage))
                self.assertGreaterEqual(f_np.coverage, 0.0)
                self.assertLessEqual(f_np.coverage, 1.0)
            if f_py is not None:
                self.assertFalse(math.isnan(f_py.coverage) or math.isinf(f_py.coverage))
                self.assertGreaterEqual(f_py.coverage, 0.0)
                self.assertLessEqual(f_py.coverage, 1.0)

        self.assertEqual(ting_times(None, sr), [])
        self.assertEqual(ting_times(np.zeros(1000, dtype=np.int16), 0), [])
        self.assertIsNone(kibble_flow(np.zeros(sr * 3, dtype=np.int16), sr, 1.0, 1.5))  # motor_sec < 1.0

    def test_adv_09_cv2_compat_fallback_full_vision_pipeline(self) -> None:
        from custom_components.molly_cody_feeder.core.analysis import vision as vision_mod

        compat_cv2 = vision_mod._build_cv2_compat()
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            bef = td_p / "bef.jpg"
            aft = td_p / "aft.jpg"
            ann = td_p / "ann.jpg"
            _write_bowl_jpg(bef, is_ir=False, with_kibble=False)
            _write_bowl_jpg(aft, is_ir=False, with_kibble=True)

            ir_bef = td_p / "ir_bef.jpg"
            ir_aft = td_p / "ir_aft.jpg"
            _write_bowl_jpg(ir_bef, is_ir=True, with_kibble=False)
            _write_bowl_jpg(ir_aft, is_ir=True, with_kibble=True)

            with mock.patch.object(vision_mod, "cv2", compat_cv2):
                analyzer = BowlAnalyzer(RuntimeConfig(captures_dir=td_p))
                res_day = analyzer.analyze_bowl_visual(str(aft), str(bef), str(ann))
                self.assertFalse(res_day.bowl_empty)
                self.assertTrue(res_day.food_increased)
                self.assertTrue(ann.is_file())

                res_ir = analyzer.analyze_bowl_visual(str(ir_aft), str(ir_bef))
                self.assertTrue(res_ir.is_infrared_mode)
                self.assertTrue(res_ir.food_increased)

                roi_img = compat_cv2.imread(str(aft))
                fill_roi, _, _ = analyzer._compute_roi_kibble_fill(roi_img[:120, :120], is_ir=False, full_img_bgr=None)
                self.assertGreaterEqual(fill_roi, 0.0)
                self.assertLessEqual(fill_roi, 1.0)

    def test_adv_10_decision_and_recording_cut_short_malformed_inputs(self) -> None:
        for bad_info in (
            None,
            "not_a_dict",
            {},
            {"recorded_wall_sec": "nan_str", "audio_sec": 5.0},
            {"recorded_wall_sec": 2.9, "audio_sec": 0.1},  # < 3.0s window ignored
            {"recorded_wall_sec": 10.0, "audio_sec": 9.0, "audio_last_rx_sec": "bad_float"},
        ):
            self.assertFalse(recording_cut_short(bad_info))  # type: ignore[arg-type]

        self.assertTrue(
            recording_cut_short({"recorded_wall_sec": 20.0, "audio_sec": 16.0, "audio_last_rx_sec": 14.5})
        )

        aud_ok = AudioAnalysisResult(True, 30, True, 25.0, 15.0, "ok", motor_duration_sec=3.0, ting_count=20, ting_rate_per_sec=6.5)
        vis_ok = VisualAnalysisResult(0.0, 0.3, 0.3, 0.3, False, True, True, True, "ok", "", "ok")
        self.assertFalse(decide_passed(aud_ok, vis_ok, feed_abnormal=2, expect_dispense=True, vision_can_override_audio=True))
        self.assertTrue(decide_passed(aud_ok, vis_ok, feed_abnormal=1, expect_dispense=True))

    def test_adv_11_knn_feedback_override_distance_and_label_inversion(self) -> None:
        from custom_components.molly_cody_feeder.core import feedback_suggests_false_alarm

        aud = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=18,
            kibble_dispensed_audio=False,
            motor_energy_db=22.0,
            impact_energy_db=12.0,
            diagnosis="borderline",
            motor_duration_sec=3.0,
            kibble_rate_per_sec=6.0,
        )
        self.assertIsNone(
            feedback_suggests_false_alarm(
                aud,
                [{"id": 1, "motor_duration_sec": 3.0, "kibble_peaks": 18, "kibble_rate": 6.0, "impact_energy_db": 12.0, "success": 0, "user_feedback": "wrong"}],
            )
        )

        rows_false_alarm = [
            {"id": 10, "motor_duration_sec": 3.1, "kibble_peaks": 19, "kibble_rate": 6.1, "impact_energy_db": 12.2, "success": 0, "user_feedback": "wrong"},
            {"id": 11, "motor_duration_sec": 2.9, "kibble_peaks": 17, "kibble_rate": 5.8, "impact_energy_db": 11.8, "success": 1, "user_feedback": "right"},
            {"id": 12, "motor_duration_sec": 3.0, "kibble_peaks": 18, "kibble_rate": 6.0, "impact_energy_db": 12.0, "success": 0, "user_feedback": "wrong"},
        ]
        note = feedback_suggests_false_alarm(aud, rows_false_alarm)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("#12", note)

        rows_mixed = list(rows_false_alarm)
        rows_mixed[2] = {"id": 12, "motor_duration_sec": 3.0, "kibble_peaks": 18, "kibble_rate": 6.0, "impact_energy_db": 12.0, "success": 0, "user_feedback": "right"}
        self.assertIsNone(feedback_suggests_false_alarm(aud, rows_mixed))

    def test_adv_12_database_same_second_refill_ordering_and_feedback_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "adv.db"))
            e1 = db.log_feed_event("scheduled_verify", "cloud", "cloud", 5, True, False, outcome="dispensed")
            r_id = db.record_refill(100, "same-second refill")
            e2 = db.log_feed_event("scheduled_verify", "cloud", "cloud", 2, True, False, outcome="dispensed")
            self.assertGreater(e2, e1)
            self.assertGreater(r_id, 0)

            hop = db.hopper_status(100)
            self.assertTrue(hop["known"])
            self.assertEqual(hop["portions_used"], 2, "Feed before refill in same second must not count against new refill")
            self.assertEqual(hop["portions_left"], 98)

            with self.assertRaises(ValueError):
                db.record_feedback("maybe_ok", event_id=e2)

            updated = db.record_feedback("false_alarm", event_id=e2, note="actually clogged")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["user_feedback"], "wrong")

    def test_adv_13_retention_manager_symlink_and_path_traversal_protection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            outside_dir = td_p / "outside_secrets"
            outside_dir.mkdir()
            secret_file = outside_dir / "tuya_session.json"
            secret_file.write_text('{"sid":"secret"}', encoding="utf-8")

            captures_dir = td_p / "captures"
            captures_dir.mkdir()
            protected_snap = captures_dir / "latest.jpg"
            protected_snap.write_bytes(b"protected_jpg")

            symlink_inside = captures_dir / "evil_link.jpg"
            symlink_inside.symlink_to(secret_file)

            cfg = RuntimeConfig(
                storage_dir=td_p,
                keep_footage=False,
                export_ml_on_prune=False,
            )
            db = KibbleDatabase(cfg.db_path)
            eid = db.log_feed_event(
                event_type="scheduled_verify",
                mode="cloud",
                transport="cloud",
                portions=1,
                success=True,
                clog_detected=False,
                before_image=str(secret_file),
                after_image=str(captures_dir / ".." / "outside_secrets" / "tuya_session.json"),
                annotated_image=str(protected_snap),
                video_clip=str(symlink_inside),
                outcome="dispensed",
            )
            rm = RetentionManager(cfg, db, captures_dir, protected=[protected_snap])
            rm.after_event(eid, True)

            self.assertTrue(secret_file.is_file(), "RetentionManager deleted a file outside work_dir via symlink/traversal!")
            self.assertTrue(protected_snap.is_file(), "RetentionManager deleted a protected file!")

    def test_adv_14_speaker_tts_cache_eviction_and_pure_python_wav_to_ulaw(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            stereo_16k = td_p / "voice16k.wav"
            sr = 16000
            samples_stereo = np.zeros(sr, dtype=np.int16)  # 0.25s stereo (4000 frames * 2 channels)
            with wave.open(str(stereo_16k), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(samples_stereo.tobytes())

            with mock.patch("shutil.which", return_value=None):
                encoded = SpeakerMixin._audio_file_to_alaw(str(stereo_16k), volume=2.0, prepend_chime=True)
            self.assertEqual(len(encoded), 8800)

            cache_dir = td_p / "tts_cache"
            cache_dir.mkdir()
            for i in range(55):
                f = cache_dir / f"old_{i:02d}.mulaw"
                f.write_bytes(b"\xff" * 900)
                os.utime(f, (1000 + i, 1000 + i))

            client = MollyFeederClient.__new__(MollyFeederClient)
            client.tts_cache_dir = cache_dir
            with mock.patch.object(
                client,
                "_synthesize_tts_file",
                side_effect=lambda txt, out, voice="": (Path(out).write_bytes(stereo_16k.read_bytes()), "mock-tts")[1],
            ):
                with mock.patch.object(client, "_audio_file_to_alaw", return_value=b"\xff" * 1600):
                    with mock.patch.object(client, "_run_session", side_effect=lambda coro: (coro.close(), {"ok": True})[1]):
                        res = client.speak_tts("Dinner time Molly!", repeat=2)
            self.assertTrue(res["ok"])
            remaining = list(cache_dir.glob("*.mulaw"))
            self.assertLessEqual(len(remaining), 50)

    def test_adv_15_meal_timing_from_schedule_weekday_bitmask_and_once_slots(self) -> None:
        from datetime import datetime
        from custom_components.molly_cody_feeder.core.service.summary import (
            meal_timing_from_schedule,
        )

        now_mon = datetime(2026, 5, 4, 10, 0, 0)
        slots = [
            {"slot": 1, "hour": 8, "minute": 0, "portions": 2, "days_mask": 0x01, "enabled": True},
            {"slot": 2, "hour": 12, "minute": 0, "portions": 3, "days_mask": 0x02, "enabled": True},
            {"slot": 3, "hour": 18, "minute": 30, "portions": 4, "days_mask": 0x00, "enabled": True},
        ]
        timing = meal_timing_from_schedule(slots, now=now_mon)
        self.assertEqual(timing["minutes_since_scheduled_meal"], 120)
        self.assertEqual(timing["next_meal_time"], "18:30")
        self.assertEqual(timing["next_meal_portions"], 4)
        self.assertEqual(timing["minutes_to_next_meal"], 8 * 60 + 30)

        self.assertIsNone(
            meal_timing_from_schedule([{"hour": 8, "minute": 0, "enabled": False}], now=now_mon)["next_meal_time"]
        )
