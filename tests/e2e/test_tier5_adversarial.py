"""Tier 5: White-Box Adversarial Hardening & Small-Compute HAOS Audio-Only Verification Suite.

Combines:
1. `TestTier5AdversarialM1CoreAndAudioOnlyHAOS` (19 tests):
   - All 15 M1 Core white-box adversarial challenges (`test_adv_01`..`test_adv_15`)
   - 4 dedicated Small-Compute HAOS Audio-Only tests (`test_adv_16`..`test_adv_19`) verifying:
     * `_ImmSessionProtocol(audio_only=True)` omitting `cmd 6` video start
     * `capture_audio_clip` & `write_wav_pure_python` with `subprocess.run` blocked
     * `< 2 MB` peak RAM verified via `tracemalloc` and 11/11 ground-truth WAV fixtures in both NumPy and pure-Python stdlib (`HAS_NUMPY=False`) modes
     * `FeedVerifier` in audio-only mode skipping vision and passing `audio_only=True`
2. `TestTier5WhiteBoxAdversarialM2M3` (16 tests):
   - All 16 M2/M3 Coordinator, Diagnostics, Services, ConfigFlow & Entity white-box adversarial tests.
"""
from __future__ import annotations

import array
import asyncio
import json
import math
import os
import socket
import struct
import tempfile
import time
import tracemalloc
import unittest
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import numpy as np
from PIL import Image

from custom_components.molly_cody_feeder import (
    DOMAIN,
    PLATFORMS,
    MollyCodyDataUpdateCoordinator,
    async_setup,
    async_setup_entry,
    async_unload_entry,
    binary_sensor as bs_mod,
    button as btn_mod,
    camera as cam_mod,
    config_flow as cf_mod,
    event as ev_mod,
    image as img_mod,
    number as num_mod,
    select as sel_mod,
    sensor as sen_mod,
    switch as sw_mod,
)
from custom_components.molly_cody_feeder._ha_compat import (
    REDACTED,
    ConfigEntry,
    HomeAssistant,
    ServiceCall,
)
from custom_components.molly_cody_feeder.const import (
    ALL_SERVICES,
    CONF_ALERT_MODE,
    CONF_CONNECTION_MODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_EATEN_CHECK_MIN,
    CONF_EMAIL,
    CONF_ENABLE_AUTO_RETRY,
    CONF_ENABLE_CAMERA,
    CONF_ENABLE_MP4_RECORDING,
    CONF_ENABLE_TTS,
    CONF_ENABLE_VERIFICATION,
    CONF_ENABLE_VISION_VERIFICATION,
    CONF_HOPPER_CAPACITY,
    CONF_HOPPER_LOW_PCT,
    CONF_LOCAL_IP,
    CONF_LOCAL_KEY,
    CONF_MAX_DAILY_RETRY,
    CONF_MEAL_PREROLL_SEC,
    CONF_NO_FOOD_HOURS,
    CONF_PASSWORD,
    CONF_PRE_MEAL_ONLINE_SEC,
    CONF_SCAN_INTERVAL,
    EVENT_ALERT,
    EVENT_FEED,
    SERVICE_CAPTURE_SNAPSHOT,
    SERVICE_CHECK_BOWL,
    SERVICE_CLEAR_CLOG_ALERT,
    SERVICE_FEED,
    SERVICE_PLAY_AUDIO,
    SERVICE_PLAY_CHIME,
    SERVICE_PLAY_TTS,
    SERVICE_REFILL_HOPPER,
    SERVICE_RUN_ONLINE_CHECK,
    SERVICE_SET_DP,
    SERVICE_SET_SCHEDULE_RAW,
    SERVICE_SET_SCHEDULE_SLOT,
    SERVICE_SYNC_SCHEDULES,
    SERVICE_VERIFY_FEED,
)
from custom_components.molly_cody_feeder.core import (
    MOTOR_NO_KIBBLE,
    AudioAnalysisResult,
    BowlAnalyzer,
    FeedVerifier,
    KibbleDatabase,
    MollyFeederClient,
    RetentionManager,
    RuntimeConfig,
    SpeakerMixin,
    VerifyRequest,
    VisualAnalysisResult,
    _alaw_to_pcm16,
    _decrypt_mqtt_22,
    _encrypt_mqtt_22,
    _imm_mac_sha1,
    _imm_seal_aes,
    _never_sent,
    _retry_if_unreached,
    decode_molly_schedule,
    decide_passed,
    feedback_suggests_false_alarm,
    kibble_flow,
    parse_days_spec,
    recording_cut_short,
    ting_times,
    update_molly_schedule_slot,
)
from custom_components.molly_cody_feeder.diagnostics import (
    async_get_config_entry_diagnostics,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

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


def _write_synthetic_feed_wav(path: Path, *, motor_on: bool = True, kibble_tings: bool = True) -> Path:
    """Write a deterministic 8 kHz mono 16-bit PCM WAV with motor hum and kibble ting spikes."""
    sample_rate = 8000
    duration_sec = 6.0
    motor_start_sec = 0.8
    motor_end_sec = 4.3
    ting_interval_sec = 0.11

    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_sec * sample_rate)
    t = np.linspace(0.0, duration_sec, n_samples, endpoint=False)
    sig = np.zeros(n_samples, dtype=np.float64)

    if motor_on:
        m_mask = (t >= motor_start_sec) & (t <= motor_end_sec)
        sig[m_mask] += 0.045 * np.sin(2.0 * np.pi * 240.0 * t[m_mask])

    if motor_on and kibble_tings:
        burst_len = int(0.025 * sample_rate)
        tb = np.arange(burst_len, dtype=np.float64) / float(sample_rate)
        window = np.hanning(burst_len)
        burst = window * (
            0.48 * np.sin(2.0 * np.pi * 1120.0 * tb)
            + 0.48 * np.sin(2.0 * np.pi * 3150.0 * tb)
        )
        for bt in np.arange(motor_start_sec + 0.2, motor_end_sec - 0.1, ting_interval_sec):
            idx = int(bt * sample_rate)
            if idx + burst_len < n_samples:
                sig[idx : idx + burst_len] += burst

    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return path


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
            arr[py1 : py1 + int((py2 - py1) * 0.55), px1:px2] = (1, 1, 1)
    Image.fromarray(arr, mode="RGB").save(str(path), format="JPEG", quality=96)


class TestTier5AdversarialM1CoreAndAudioOnlyHAOS(unittest.TestCase):
    """19 White-box adversarial & Small-Compute HAOS Audio-Only tests for M1 Core subsystems."""

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

    def test_adv_16_imm_session_protocol_audio_only_omits_cmd6_video_start(self) -> None:
        from custom_components.molly_cody_feeder.core.molly.imm_codec import (
            _IMM_AUDIO_ONLY_STARTUP_MESSAGES,
            _IMM_CMD_START_AUDIO,
            _IMM_CMD_START_VIDEO,
            _IMM_STARTUP_MESSAGES,
        )
        from custom_components.molly_cody_feeder.core.molly.imm_transport import (
            _ImmSessionProtocol,
        )

        self.assertGreater(len(_IMM_STARTUP_MESSAGES), len(_IMM_AUDIO_ONLY_STARTUP_MESSAGES))
        audio_only_cmds = [struct.unpack_from("<I", m, 12)[0] for m in _IMM_AUDIO_ONLY_STARTUP_MESSAGES]
        self.assertNotIn(_IMM_CMD_START_VIDEO, audio_only_cmds)
        self.assertIn(_IMM_CMD_START_AUDIO, audio_only_cmds)

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

    def test_adv_17_capture_audio_clip_and_write_wav_pure_python_subprocess_blocked(self) -> None:
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

            def _fake_imm_session(**kwargs: Any) -> Dict[str, Any]:
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

            def _run_fake_coro(coro: Any) -> Dict[str, Any]:
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

    def test_adv_18_small_compute_haos_audio_only_11_fixtures_and_tracemalloc_ram_cap(self) -> None:
        from custom_components.molly_cody_feeder.core.analysis import (
            _analyze_audio_stdlib,
            _analyze_flow_stdlib,
            _analyze_tings_stdlib,
            audio as audio_mod,
        )

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            analyzer = BowlAnalyzer(RuntimeConfig(captures_dir=td_p))

            wav_ok = td_p / "dispense_ok.wav"
            wav_clog = td_p / "dispense_clog.wav"
            wav_silent = td_p / "dispense_silent.wav"
            s_ok = _write_synthetic_wav(wav_ok, motor_sec=3.0, with_tings=True)
            s_clog = _write_synthetic_wav(wav_clog, motor_sec=3.0, with_tings=False)
            _write_synthetic_wav(wav_silent, motor_sec=0.0, with_tings=False)

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

            # Verify all 11 ground-truth WAV fixtures in both NumPy and pure-Python stdlib (HAS_NUMPY=False) modes
            clips_dir = REPO_ROOT / "tests" / "fixtures" / "labeled_clips"
            labels_file = clips_dir / "training_labels.json"
            self.assertTrue(labels_file.is_file(), "Ground-truth training_labels.json must exist")
            labels = json.loads(labels_file.read_text(encoding="utf-8"))
            reviewed_count = 0
            for rec in labels:
                if not rec.get("user_reviewed"):
                    continue
                wav_f = clips_dir / f"{rec['clip_id']}.wav"
                self.assertTrue(wav_f.is_file(), f"Missing fixture WAV: {wav_f}")
                reviewed_count += 1
                expected_ok = bool(rec["feed_success"])

                # 1. NumPy path (< 2 MB peak memory)
                tracemalloc.start()
                res_np = analyzer.analyze_dispense_audio(str(wav_f))
                _, peak_np = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertTrue(res_np.motor_detected, f"NumPy motor_detected on {rec['clip_id']}")
                self.assertEqual(res_np.kibble_dispensed_audio, expected_ok, f"NumPy verdict on {rec['clip_id']}")
                self.assertLess(peak_np, 2 * 1024 * 1024, f"NumPy peak RAM >= 2MB on {rec['clip_id']}: {peak_np}")

                # 2. Pure-Python stdlib fallback path (HAS_NUMPY=False, < 2 MB peak memory)
                tracemalloc.start()
                with mock.patch.object(audio_mod, "HAS_NUMPY", False):
                    res_py = analyzer.analyze_dispense_audio(str(wav_f))
                _, peak_py = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                self.assertTrue(res_py.motor_detected, f"Stdlib motor_detected on {rec['clip_id']}")
                self.assertEqual(res_py.kibble_dispensed_audio, expected_ok, f"Stdlib verdict on {rec['clip_id']}")
                self.assertLess(peak_py, 2 * 1024 * 1024, f"Stdlib peak RAM >= 2MB on {rec['clip_id']}: {peak_py}")

            self.assertEqual(reviewed_count, 11, "Must verify all 11 ground-truth labeled WAV fixtures")

    def test_adv_19_feed_verifier_audio_only_mode_skips_vision_and_mp4(self) -> None:
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

            def _fake_audio_only_capture(**kw: Any) -> Dict[str, Any]:
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


class TestTier5WhiteBoxAdversarialM2M3(unittest.IsolatedAsyncioTestCase):
    """16 White-box adversarial test cases targeting M2, M3, Coordinator, Diagnostics, Services & Entities."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hass = HomeAssistant(config_dir=self.tmp_path)
        self.entry = ConfigEntry(
            entry_id="entry_adv_1",
            unique_id="dev_adv_001",
            title="Adversarial Feeder",
            data={
                CONF_EMAIL: "owner_secret@example.com",
                CONF_PASSWORD: "SuperSecretPassword!99",
                CONF_DEVICE_ID: "dev_adv_001",
                CONF_DEVICE_NAME: "Adversarial Feeder",
                CONF_LOCAL_IP: "192.168.1.155",
                CONF_LOCAL_KEY: "LocalKeySecret16",
                CONF_CONNECTION_MODE: "auto",
                CONF_ENABLE_CAMERA: False,
                CONF_ENABLE_VERIFICATION: True,
                CONF_ENABLE_VISION_VERIFICATION: False,
                CONF_ENABLE_MP4_RECORDING: False,
                CONF_ENABLE_TTS: True,
                CONF_ENABLE_AUTO_RETRY: True,
            },
            options={
                CONF_ENABLE_CAMERA: False,
                CONF_ENABLE_VERIFICATION: True,
                CONF_ENABLE_VISION_VERIFICATION: False,
                CONF_ENABLE_MP4_RECORDING: False,
                CONF_ENABLE_TTS: True,
                CONF_ENABLE_AUTO_RETRY: True,
                CONF_MAX_DAILY_RETRY: 3,
            },
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _make_mocked_coordinator(self) -> MollyCodyDataUpdateCoordinator:
        with mock.patch(
            "custom_components.molly_cody_feeder.core.molly.MollyFeederClient.get_status",
            return_value={
                "device_id": "dev_adv_001",
                "device_name": "Adversarial Feeder",
                "configured_mode": "auto",
                "active_transport": "lan",
                "local_ip": "192.168.1.155",
                "online": True,
                "dps": {
                    "101": True,
                    "103": False,
                    "104": True,
                    "106": "1",
                    "108": "0",
                    "134": True,
                    "201": 1,
                    "202": 320,
                    "207": "7f07000210000007f1800031000000",
                    "255": 1,
                },
                "named_dps": {},
                "food_weight_g": 320,
                "last_feed_portions": 1,
                "feed_abnormal": 1,
                "schedule_raw": "7f07000210000007f1800031000000",
            },
        ):
            coord = MollyCodyDataUpdateCoordinator(self.hass, self.entry)
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = {"coordinator": coord}
        self.entry.runtime_data = coord
        return coord

    # 1. Audio-Only HAOS Mode: Verified Dispense & Motor-No-Kibble Clog with Zero Camera/Vision
    async def test_adv_01_audio_only_haos_verify_feed_dispensed_and_clog(self) -> None:
        coord = self._make_mocked_coordinator()
        self.assertFalse(coord.runtime_config.enable_camera)
        self.assertFalse(coord.runtime_config.enable_vision_verification)
        self.assertTrue(coord.runtime_config.enable_verification)

        captured_kwargs: List[Dict[str, Any]] = []

        def _fake_capture_dispensed(**kwargs: Any) -> Dict[str, Any]:
            captured_kwargs.append(dict(kwargs))
            if callable(kwargs.get("trigger_fn")):
                kwargs["trigger_fn"]()
            wav_p = Path(kwargs["output_wav"])
            _write_synthetic_feed_wav(wav_p, motor_on=True, kibble_tings=True)
            return {
                "audio_ok": True,
                "wav_saved": True,
                "recorded_wall_sec": 6.0,
                "audio_sec": 6.0,
                "audio_last_rx_sec": 5.9,
            }

        coord.client.dispense_food = mock.MagicMock(return_value=(True, "lan"))
        coord.client.capture_av_clip = mock.MagicMock(side_effect=_fake_capture_dispensed)
        coord.analyzer.analyze_bowl_visual = mock.MagicMock()

        res_ok = await coord.async_verify_feed(portions=2, trigger="manual_dispense_verify")
        self.assertTrue(res_ok["passed"])
        self.assertEqual(res_ok["outcome"], "dispensed")
        self.assertFalse(res_ok["clog_detected"])
        self.assertEqual(len(captured_kwargs), 1)
        self.assertTrue(captured_kwargs[0].get("audio_only"))
        self.assertFalse(captured_kwargs[0].get("decode_video", True))
        self.assertIsNone(captured_kwargs[0].get("before_jpg"))
        self.assertIsNone(captured_kwargs[0].get("after_jpg"))
        self.assertEqual(captured_kwargs[0].get("output_mp4"), "")
        coord.analyzer.analyze_bowl_visual.assert_not_called()

        # Now test motor_no_kibble in Audio-Only HAOS mode
        def _fake_capture_clog(**kwargs: Any) -> Dict[str, Any]:
            if callable(kwargs.get("trigger_fn")):
                kwargs["trigger_fn"]()
            wav_p = Path(kwargs["output_wav"])
            _write_synthetic_feed_wav(wav_p, motor_on=True, kibble_tings=False)
            return {
                "audio_ok": True,
                "wav_saved": True,
                "recorded_wall_sec": 6.0,
                "audio_sec": 6.0,
                "audio_last_rx_sec": 5.9,
            }

        coord.client.capture_av_clip = mock.MagicMock(side_effect=_fake_capture_clog)
        res_clog = await coord.async_verify_feed(portions=2, trigger="manual_dispense_verify")
        self.assertFalse(res_clog["passed"])
        self.assertEqual(res_clog["outcome"], "motor_no_kibble")
        self.assertTrue(res_clog["clog_detected"])
        self.assertTrue(coord.data["summary"]["clog_detected"])
        self.assertEqual(coord.data["summary"]["status"], "problem")
        coord.analyzer.analyze_bowl_visual.assert_not_called()

    # 2. Vision Verification Forced False When Camera Disabled (Options Sync Guard)
    async def test_adv_02_enable_vision_forced_false_when_camera_disabled(self) -> None:
        coord = self._make_mocked_coordinator()
        await coord.async_set_option(CONF_ENABLE_CAMERA, False)
        await coord.async_set_option(CONF_ENABLE_VISION_VERIFICATION, True)
        # Even if user sets enable_vision_verification=True while enable_camera=False, runtime_config forces False
        self.assertFalse(coord.runtime_config.enable_camera)
        self.assertFalse(coord.runtime_config.enable_vision_verification)

        # Turning camera on allows vision verification to become active
        await coord.async_set_option(CONF_ENABLE_CAMERA, True)
        self.assertTrue(coord.runtime_config.enable_camera)
        self.assertTrue(coord.runtime_config.enable_vision_verification)

    # 3. Auto-Retry Daily Cap Exhaustion Mid-Loop & Hopper Empty Suspected Stop
    async def test_adv_03_auto_retry_daily_cap_and_hopper_empty_suspected_stop(self) -> None:
        coord = self._make_mocked_coordinator()
        coord.runtime_config.max_retry_attempts = 3
        await coord.async_set_option(CONF_MAX_DAILY_RETRY, 2)

        # Case A: Both scheduled feed and first retry return motor_no_kibble -> immediate hopper_empty_suspected stop
        calls: List[str] = []

        async def _fake_verify_double_motor(portions: int = 1, trigger: str = "", **kw: Any) -> Dict[str, Any]:
            calls.append(trigger)
            coord.db.log_feed_event(
                event_type=trigger,
                mode="auto",
                transport="lan",
                portions=portions,
                success=False,
                clog_detected=True,
                audio_state="motor_only_clog",
                motor_detected=True,
                outcome="motor_no_kibble",
            )
            return {"ok": False, "passed": False, "outcome": "motor_no_kibble", "clog_detected": True}

        with mock.patch.object(coord, "async_verify_feed", side_effect=_fake_verify_double_motor):
            res = await coord.async_run_scheduled_meal(portions=3)

        # Must stop after 1st retry (total 2 calls: scheduled_verify + 1 auto_retry_verify), NOT all 3 retry attempts
        self.assertEqual(calls, ["scheduled_verify", "auto_retry_verify"])
        self.assertTrue(res.get("hopper_empty_suspected"))
        self.assertEqual(res.get("alert_event"), "hopper_empty_suspected")
        self.assertEqual(coord.data["last_alert_event"]["event_type"], "hopper_empty_suspected")

        # Case B: Scheduled meal has no_motor; 1st retry already used 1 portion earlier, 2nd retry hits cap=2
        calls.clear()

        async def _fake_verify_no_motor(portions: int = 1, trigger: str = "", **kw: Any) -> Dict[str, Any]:
            calls.append(trigger)
            coord.db.log_feed_event(
                event_type=trigger,
                mode="auto",
                transport="lan",
                portions=portions,
                success=False,
                clog_detected=True,
                audio_state="no_motor",
                motor_detected=False,
                outcome="no_motor",
            )
            return {"ok": False, "passed": False, "outcome": "no_motor", "clog_detected": True}

        with mock.patch.object(coord, "async_verify_feed", side_effect=_fake_verify_no_motor):
            await coord.async_run_scheduled_meal(portions=2)

        # 1 retry portion was already logged in Case A; Case B runs 1 more retry (reaching 2/2 cap) then hits retry_budget_exhausted
        self.assertEqual(calls, ["scheduled_verify", "auto_retry_verify"])
        self.assertEqual(coord.data["last_alert_event"]["event_type"], "retry_budget_exhausted")
        self.assertEqual(coord.db.portions_today("auto_retry_verify"), 2)

    # 4. Shared Session Lock Contention Between Verify Feed, Snapshot, and TTS
    async def test_adv_04_shared_session_lock_contention_camera_tts_and_verify(self) -> None:
        coord = self._make_mocked_coordinator()
        await coord.async_set_option(CONF_ENABLE_CAMERA, True)
        await coord.async_set_option(CONF_ENABLE_TTS, True)
        coord._latest_snapshot_bytes = b"\xff\xd8\xff\xe0cached_snap"
        coord._refresh_summary_in_place()

        cam_entities: List[Any] = []
        await cam_mod.async_setup_entry(self.hass, self.entry, cam_entities.extend)
        cam_entity = cam_entities[0]

        order: List[str] = []
        verify_entered = asyncio.Event()
        allow_verify_finish = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _blocking_verifier_run(req: Any) -> Any:
            order.append("verify_start")
            loop.call_soon_threadsafe(verify_entered.set)
            deadline = time.time() + 3.0
            while not allow_verify_finish.is_set() and time.time() < deadline:
                time.sleep(0.01)
            order.append("verify_end")
            return True, {"outcome": "dispensed", "clog_detected": False}

        coord.verifier.run = _blocking_verifier_run  # type: ignore[method-assign]
        coord.client.capture_snapshot = mock.MagicMock(side_effect=lambda p: order.append("live_snap") or True)
        coord.client.speak_tts = mock.MagicMock(side_effect=lambda **k: order.append("tts_run") or {"ok": True})

        verify_task = asyncio.create_task(coord.async_verify_feed(portions=1))
        await verify_entered.wait()

        # While verify_task holds _session_lock:
        self.assertTrue(coord.session_lock.locked())
        self.assertTrue(coord._busy)

        # 1) Camera entity and coordinator.async_capture_snapshot must NOT block or call client.capture_snapshot; they return cached bytes immediately
        snap_res = await cam_entity.async_camera_image()
        self.assertEqual(snap_res, b"\xff\xd8\xff\xe0cached_snap")
        coord.client.capture_snapshot.assert_not_called()

        # 2) TTS waits on the shared lock until verify finishes
        tts_task = asyncio.create_task(coord.async_play_tts(message="Testing lock"))
        await asyncio.sleep(0.03)
        self.assertFalse(tts_task.done())

        allow_verify_finish.set()
        await verify_task
        await tts_task
        self.assertEqual(order, ["verify_start", "verify_end", "tts_run"])
        self.assertFalse(coord.session_lock.locked())
        self.assertFalse(coord._busy)

    # 5. Deeply Nested Diagnostics Secret Redaction & Value-Level Substring Scrubbing
    async def test_adv_05_diagnostics_deep_nested_and_substring_secret_redaction(self) -> None:
        coord = self._make_mocked_coordinator()
        coord.client.sid = "sid_secret_token_987654"
        coord.client.uid = "uid_secret_123456"
        coord.client.ecode = "ecode_secret_abcdef"
        coord.runtime_config.gemini_api_key = "AIzaSySecretGeminiKey999"
        coord.runtime_config.discord_webhook_url = "https://discord.com/api/webhooks/secret_hook"

        # Inject secrets into deeply nested dicts, lists, tuples, Paths, and embedded inside log strings
        coord.data["nested_debug"] = {
            "level1": [
                {
                    "password": "inline_other_password",
                    "localKey": "inline_other_local_key",
                    "leak_in_url": "https://api.example.com/auth?sid=sid_secret_token_987654&user=owner_secret@example.com",
                    "tuple_data": ("SafeText", "SuperSecretPassword!99", Path("/tmp/LocalKeySecret16/file.jpg")),
                }
            ]
        }
        coord.data["latest_snapshot_bytes"] = b"\xff\xd8\xff\xe0\x00\x10JFIF"

        diag = await async_get_config_entry_diagnostics(self.hass, self.entry)
        serialized = json.dumps(diag)

        # Verify raw secrets never appear anywhere in serialized JSON
        for forbidden in (
            "owner_secret@example.com",
            "SuperSecretPassword!99",
            "LocalKeySecret16",
            "sid_secret_token_987654",
            "uid_secret_123456",
            "ecode_secret_abcdef",
            "AIzaSySecretGeminiKey999",
            "https://discord.com/api/webhooks/secret_hook",
            "inline_other_password",
            "inline_other_local_key",
        ):
            self.assertNotIn(forbidden, serialized, f"Secret leaked in diagnostics: {forbidden}")

        self.assertEqual(diag["entry"]["data"]["password"], REDACTED)
        self.assertEqual(diag["entry"]["data"]["local_key"], REDACTED)
        self.assertEqual(diag["entry"]["data"]["email"], REDACTED)
        self.assertEqual(diag["client"]["sid"], REDACTED)
        self.assertEqual(diag["client"]["uid"], REDACTED)
        self.assertEqual(diag["client"]["ecode"], REDACTED)
        self.assertEqual(diag["status"]["latest_snapshot_bytes"], 10)
        nested_item = diag["status"]["nested_debug"]["level1"][0]
        self.assertEqual(nested_item["password"], REDACTED)
        self.assertEqual(nested_item["localKey"], REDACTED)
        self.assertIn(REDACTED, nested_item["leak_in_url"])

    # 6. Multi-Entry Service Target Filtering (`entry_id` and `device_id`)
    async def test_adv_06_service_routing_multi_entry_and_device_id_isolation(self) -> None:
        entry2 = ConfigEntry(
            entry_id="entry_adv_2",
            unique_id="dev_adv_002",
            title="Second Feeder",
            data={CONF_DEVICE_ID: "dev_adv_002", CONF_DEVICE_NAME: "Second Feeder"},
        )
        coord1 = self._make_mocked_coordinator()
        with mock.patch(
            "custom_components.molly_cody_feeder.core.molly.MollyFeederClient.get_status",
            return_value={"device_id": "dev_adv_002", "device_name": "Second Feeder", "online": True, "dps": {}},
        ):
            coord2 = MollyCodyDataUpdateCoordinator(self.hass, entry2)

        self.hass.data.setdefault(DOMAIN, {})["entry_adv_1"] = {"coordinator": coord1}
        self.hass.data[DOMAIN]["entry_adv_2"] = {"coordinator": coord2}
        await async_setup(self.hass, {})

        coord1.async_feed = mock.AsyncMock(return_value={"ok": True, "device": "1"})
        coord2.async_feed = mock.AsyncMock(return_value={"ok": True, "device": "2"})

        # Target only device_id="dev_adv_002"
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_FEED,
            {"portions": 4, "verify": False, "device_id": "dev_adv_002"},
        )
        coord1.async_feed.assert_not_called()
        coord2.async_feed.assert_awaited_once_with(portions=4, verify=False)

        coord2.async_feed.reset_mock()
        # Target only entry_id="entry_adv_1"
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_FEED,
            {"portions": 2, "verify": True, "entry_id": "entry_adv_1"},
        )
        coord1.async_feed.assert_awaited_once_with(portions=2, verify=True)
        coord2.async_feed.assert_not_called()

    # 7. All 14 Services Under Malformed / Boundary / Alias Inputs
    async def test_adv_07_all_14_services_boundary_and_alias_inputs(self) -> None:
        coord = self._make_mocked_coordinator()
        await async_setup(self.hass, {})

        coord.client.dispense_food = mock.MagicMock(return_value=(True, "lan"))
        coord.client.set_dp = mock.MagicMock(return_value=(True, "lan"))
        coord.client.set_schedule_raw = mock.MagicMock(return_value=(True, "lan"))
        coord.client.update_schedule_slot = mock.MagicMock(return_value=(True, "lan", []))
        coord.client.speak_tts = mock.MagicMock(return_value={"ok": True})
        coord.client.play_audio_file = mock.MagicMock(return_value={"ok": True})
        coord.client.play_chime = mock.MagicMock(return_value={"ok": True})

        # 1) feed with out-of-range portions (999 -> clamped to 20, -5 -> clamped to 1)
        res_feed_hi = await self.hass.services.async_call(DOMAIN, SERVICE_FEED, {"portions": 999, "verify": False})
        self.assertEqual(res_feed_hi["portions"], 20)
        res_feed_lo = await self.hass.services.async_call(DOMAIN, SERVICE_FEED, {"portions": -5, "verify": False})
        self.assertEqual(res_feed_lo["portions"], 1)

        # 2) set_schedule_slot using "time": "06:45" string parsing and portions clamping
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SCHEDULE_SLOT,
            {"slot": 2, "time": "06:45", "portions": 50, "days": "weekdays", "enabled": True},
        )
        slot2 = [s for s in coord.data["schedule"] if s["slot"] == 2][0]
        self.assertEqual(slot2["hour"], 6)
        self.assertEqual(slot2["minute"], 45)
        self.assertEqual(slot2["portions"], 20)

        # 3) set_schedule_slot with invalid slot 0 or 9 raises ValueError
        with self.assertRaises(ValueError):
            await self.hass.services.async_call(DOMAIN, SERVICE_SET_SCHEDULE_SLOT, {"slot": 0})
        with self.assertRaises(ValueError):
            await self.hass.services.async_call(DOMAIN, SERVICE_SET_SCHEDULE_SLOT, {"slot": 9})

        # 4) refill_hopper with 0 or negative capacity falls back to configured default (240)
        hop_res = await self.hass.services.async_call(
            DOMAIN, SERVICE_REFILL_HOPPER, {"capacity_portions": -10, "note": "negative test"}
        )
        self.assertEqual(hop_res["capacity_portions"], 240)

        # 5) play_tts with alias "text" and repeat=0 -> repeat clamped to 1
        await self.hass.services.async_call(
            DOMAIN, SERVICE_PLAY_TTS, {"text": "Hello pups", "repeat": 0, "volume": "3.0"}
        )
        coord.client.speak_tts.assert_called_with(
            text="Hello pups", voice="en-US-AriaNeural", volume=3.0, repeat=1, prepend_chime=False
        )

        # 6) play_audio with alias "path"
        await self.hass.services.async_call(DOMAIN, SERVICE_PLAY_AUDIO, {"path": "/tmp/test.wav", "volume": 1.5})
        coord.client.play_audio_file.assert_called_with(audio_path="/tmp/test.wav", volume=1.5, prepend_chime=False)

        # 7) set_dp with DP 255 string "4" updates feed_abnormal int to 4 and sets status="problem"
        await self.hass.services.async_call(DOMAIN, SERVICE_SET_DP, {"dp": 255, "value": "4"})
        self.assertEqual(coord.data["feed_abnormal"], 4)
        self.assertEqual(coord.data["summary"]["status"], "problem")

        # 8) clear_clog_alert keeps status="problem" if hardware fault DP 255 > 1 is still active
        await self.hass.services.async_call(DOMAIN, SERVICE_CLEAR_CLOG_ALERT, {})
        self.assertFalse(coord.data["summary"]["clog_detected"])
        self.assertEqual(coord.data["summary"]["status"], "problem")

        # Reset DP 255 to 1 (normal) -> status recovers to "ok"
        await self.hass.services.async_call(DOMAIN, SERVICE_SET_DP, {"dp_id": 255, "value": 1})
        self.assertEqual(coord.data["summary"]["status"], "ok")

    # 8. Unload / Reload Cycle Cleans Up Timers, Eaten-Check Callbacks, and `hass.data`
    async def test_adv_08_entry_unload_and_reload_cleans_up_timers_and_listeners(self) -> None:
        with mock.patch(
            "custom_components.molly_cody_feeder.core.molly.MollyFeederClient.get_status",
            return_value={
                "device_id": "dev_adv_001",
                "device_name": "Adversarial Feeder",
                "online": True,
                "dps": {"207": "7f07000210000007f1800031000000", "255": 1},
                "schedule_raw": "7f07000210000007f1800031000000",
            },
        ):
            ok = await async_setup_entry(self.hass, self.entry)
            self.assertTrue(ok)
            coord: MollyCodyDataUpdateCoordinator = self.hass.data[DOMAIN][self.entry.entry_id]["coordinator"]
            coord._schedule_eaten_check_callback(0.35)
            self.assertGreater(len(coord._unsub_timers), 0)
            self.assertIsNotNone(coord._unsub_eaten_check)

            # Unload entry
            unloaded = await async_unload_entry(self.hass, self.entry)
            self.assertTrue(unloaded)
            self.assertNotIn(self.entry.entry_id, self.hass.data[DOMAIN])
            self.assertEqual(len(coord._unsub_timers), 0)
            self.assertIsNone(coord._unsub_eaten_check)

            # Reload entry cleanly
            ok2 = await async_setup_entry(self.hass, self.entry)
            self.assertTrue(ok2)
            self.assertIn(self.entry.entry_id, self.hass.data[DOMAIN])
            await async_unload_entry(self.hass, self.entry)

    # 9. All 9 Entity Platforms Survive `coordinator.data = None` and Corrupted `coordinator.data`
    async def test_adv_09_all_9_entity_platforms_survive_none_or_corrupt_coordinator_data(self) -> None:
        coord = self._make_mocked_coordinator()

        all_entities: List[Any] = []
        for mod in (sen_mod, bs_mod, sw_mod, btn_mod, num_mod, sel_mod, cam_mod, img_mod, ev_mod):
            await mod.async_setup_entry(self.hass, self.entry, all_entities.extend)

        # Total entities across 9 platforms: 22 + 9 + 16 + 8 + 12 + 4 + 1 + 3 + 2 = 77
        self.assertEqual(len(all_entities), 77)

        # Corrupt coordinator.data to None, then to non-dict types inside dict
        for corrupt_state in (
            None,
            {},
            {"summary": None, "dps": "not_a_dict", "schedule": "not_a_list", "options": None},
        ):
            coord.data = corrupt_state  # type: ignore[assignment]
            for ent in all_entities:
                _ = ent.unique_id
                _ = ent.name
                _ = ent.device_info
                _ = ent.extra_state_attributes
                if hasattr(ent, "native_value"):
                    val = ent.native_value
                    if isinstance(ent, num_mod.NumberEntity):
                        self.assertIsNotNone(val)
                if hasattr(ent, "is_on"):
                    self.assertIsInstance(ent.is_on, bool)
                if hasattr(ent, "current_option"):
                    self.assertIsInstance(ent.current_option, str)
                if hasattr(ent, "image"):
                    self.assertIsNone(ent.image())
                if hasattr(ent, "state"):
                    _ = ent.state

    # 10. EventEntity Duplicate Bus Subscription Guard & Fallback Unknown Event Type Coercion
    async def test_adv_10_event_entity_bus_lifecycle_and_unknown_event_type_fallback(self) -> None:
        coord = self._make_mocked_coordinator()

        ev_entities: List[Any] = []
        await ev_mod.async_setup_entry(self.hass, self.entry, ev_entities.extend)
        feed_ev_ent, alert_ev_ent = ev_entities[0], ev_entities[1]

        # Calling async_added_to_hass multiple times must not register duplicate listeners
        await feed_ev_ent.async_added_to_hass()
        await feed_ev_ent.async_added_to_hass()
        self.assertEqual(len(self.hass.bus._listeners.get(EVENT_FEED, [])), 1)

        # Firing an unknown event_type coerces _last_event_type to default_event_type ("feed_event" / "alert_event")
        self.hass.bus.async_fire(EVENT_FEED, {"event_type": "totally_custom_outcome", "portions": 5})
        self.assertEqual(feed_ev_ent._last_event_type, "feed_event")
        self.assertEqual(feed_ev_ent.extra_state_attributes.get("portions"), 5)

        self.hass.bus.async_fire(EVENT_ALERT, {"event_type": "unexpected_custom_alert", "urgent": True})
        self.assertEqual(alert_ev_ent._last_event_type, "alert_event")
        self.assertTrue(alert_ev_ent.extra_state_attributes.get("urgent"))

        # Removing from hass unsubscribes cleanly
        await feed_ev_ent.async_will_remove_from_hass()
        self.assertEqual(len(self.hass.bus._listeners.get(EVENT_FEED, [])), 0)

    # 11. ImageEntity Digest Tracking Updates `image_last_updated` Only When Bytes Change
    async def test_adv_11_image_entity_digest_updates_timestamp_only_on_change(self) -> None:
        coord = self._make_mocked_coordinator()

        img_entities: List[Any] = []
        await img_mod.async_setup_entry(self.hass, self.entry, img_entities.extend)
        ann_ent = img_entities[0]

        self.assertIsNone(ann_ent.image())
        coord.data["latest_annotated_bytes"] = b"\xff\xd8\xff\xe0frame_one"
        b1 = await ann_ent.async_image()
        self.assertEqual(b1, b"\xff\xd8\xff\xe0frame_one")
        ts1 = ann_ent.image_last_updated
        self.assertIsNotNone(ts1)

        # Calling image() again with identical bytes preserves timestamp
        with mock.patch.object(img_mod.dt_util, "utcnow", return_value=ts1):
            _ = ann_ent.image()
        self.assertEqual(ann_ent.image_last_updated, ts1)

        # Updating bytes updates digest and timestamp
        coord.data["latest_annotated_bytes"] = b"\xff\xd8\xff\xe0frame_two"
        b2 = await ann_ent.async_image()
        self.assertEqual(b2, b"\xff\xd8\xff\xe0frame_two")
        self.assertNotEqual(ann_ent._last_bytes_digest, hash(b"\xff\xd8\xff\xe0frame_one"))

    # 12. Same-Timestamp Multiple Schedule Slots Portions Aggregation in `_sync_schedule_timers`
    async def test_adv_12_schedule_timers_aggregate_portions_for_identical_slot_times(self) -> None:
        coord = self._make_mocked_coordinator()
        # Two enabled slots both scheduled at 08:30 (Slot 1 = 2 portions, Slot 2 = 3 portions)
        raw_two_same_time = "7f08300210000007f0830031000000"
        meals = decode_molly_schedule(raw_two_same_time)

        tracked_calls: List[Dict[str, Any]] = []
        with mock.patch(
            "custom_components.molly_cody_feeder.coordinator.async_track_time_change",
            side_effect=lambda hass, cb, **kw: tracked_calls.append({"cb": cb, **kw}) or (lambda: None),
        ):
            coord._sync_schedule_timers(meals)

        # 1 meal callback + 1 pre-meal online callback for the unique (08, 30) time
        self.assertEqual(len(tracked_calls), 2)
        meal_cb = tracked_calls[0]["cb"]
        coord.async_run_scheduled_meal = mock.AsyncMock(return_value={"ok": True})
        await meal_cb()
        # Portions must be aggregated: 2 + 3 = 5 portions
        coord.async_run_scheduled_meal.assert_awaited_once_with(portions=5)

    # 13. ConfigFlow LAN Step & Hybrid Mode Normalization Edge Cases
    async def test_adv_13_config_flow_step_lan_and_hybrid_mode_normalization(self) -> None:
        flow = cf_mod.MollyCodyConfigFlow()
        flow.hass = self.hass

        # Show LAN form
        form_res = await flow.async_step_lan(None)
        self.assertEqual(form_res["type"], "form")
        self.assertEqual(form_res["step_id"], "lan")

        # Submit LAN step with valid mocked LAN status
        with mock.patch(
            "custom_components.molly_cody_feeder.config_flow.MollyFeederClient.get_status",
            return_value={
                "online": True,
                "device_id": "lan_dev_777",
                "device_name": "LAN Only Feeder",
                "local_ip": "192.168.1.200",
                "dps": {"201": 1},
            },
        ):
            create_res = await flow.async_step_lan(
                {
                    CONF_DEVICE_ID: "lan_dev_777",
                    CONF_DEVICE_NAME: "LAN Only Feeder",
                    CONF_LOCAL_IP: "192.168.1.200",
                    CONF_LOCAL_KEY: "lan_secret_key_1",
                    CONF_ENABLE_CAMERA: False,
                    CONF_ENABLE_VERIFICATION: True,
                }
            )
        self.assertEqual(create_res["type"], "create_entry")
        self.assertEqual(create_res["data"][CONF_CONNECTION_MODE], "lan")
        self.assertEqual(create_res["data"][CONF_DEVICE_ID], "lan_dev_777")
        self.assertFalse(create_res["data"][CONF_ENABLE_CAMERA])

        # OptionsFlow normalizes "hybrid" -> "auto"
        opt_flow = cf_mod.MollyCodyConfigFlow.async_get_options_flow(self.entry)
        opt_res = await opt_flow.async_step_init({CONF_CONNECTION_MODE: "hybrid", CONF_SCAN_INTERVAL: 45})
        self.assertEqual(opt_res["type"], "create_entry")
        self.assertEqual(opt_res["data"][CONF_CONNECTION_MODE], "auto")
        self.assertEqual(opt_res["data"][CONF_SCAN_INTERVAL], 45)

    # 14. Post-Meal Eaten Check Boundary: IR Night Mode Ignores Darkness vs Daylight Threshold Alert
    async def test_adv_14_eaten_check_ir_mode_skip_and_daylight_uneaten_alert(self) -> None:
        coord = self._make_mocked_coordinator()
        await coord.async_set_option(CONF_ENABLE_CAMERA, True)

        def _write_dummy_snap(dest: str) -> bool:
            Path(dest).write_bytes(b"\xff\xd8\xff\xe0dummy")
            return True

        coord.client.capture_snapshot = mock.MagicMock(side_effect=_write_dummy_snap)

        # In IR mode (is_ir=True), async_run_eaten_check must return False without firing food_not_eaten alert
        coord.analyzer.bowl_fill = mock.MagicMock(return_value=(0.40, True))
        eaten_ir = await coord.async_run_eaten_check(after_meal_fill=0.40)
        self.assertFalse(eaten_ir)
        self.assertIsNone(coord.data["last_alert_event"])

        # In daylight mode, when fill (0.35) >= 0.7 * after_meal_fill (0.28), food is NOT eaten -> fires food_not_eaten alert
        coord.analyzer.bowl_fill = mock.MagicMock(return_value=(0.35, False))
        eaten_day_full = await coord.async_run_eaten_check(after_meal_fill=0.40)
        self.assertFalse(eaten_day_full)
        self.assertTrue(coord.data["summary"]["food_present"])
        self.assertEqual(coord.data["last_alert_event"]["event_type"], "food_not_eaten")

        # When fill drops to 0.05 (< 0.28), food IS eaten -> food_present becomes False
        coord.analyzer.bowl_fill = mock.MagicMock(return_value=(0.05, False))
        eaten_day_empty = await coord.async_run_eaten_check(after_meal_fill=0.40)
        self.assertTrue(eaten_day_empty)
        self.assertFalse(coord.data["summary"]["food_present"])

    # 15. Select & Number Entities Out-of-Range / Raw DP Code Coercion
    async def test_adv_15_select_and_number_entities_boundary_coercion(self) -> None:
        coord = self._make_mocked_coordinator()
        coord.client.set_dp = mock.MagicMock(return_value=(True, "lan"))

        sel_entities: List[Any] = []
        await sel_mod.async_setup_entry(self.hass, self.entry, sel_entities.extend)
        by_key_sel = {e.key: e for e in sel_entities}

        # Selecting raw DP value "2" or invalid label "invalid_mode" on nightvision select
        nv_sel = by_key_sel["nightvision"]
        await nv_sel.async_select_option("2")
        self.assertEqual(coord.data["dps"]["108"], "2")
        self.assertEqual(nv_sel.current_option, "on")

        await nv_sel.async_select_option("nonexistent_option")
        self.assertEqual(coord.data["dps"]["108"], "0")
        self.assertEqual(nv_sel.current_option, "auto")

        # Number entities clamp out-of-range values
        num_entities: List[Any] = []
        await num_mod.async_setup_entry(self.hass, self.entry, num_entities.extend)
        by_key_num = {e.key: e for e in num_entities}

        tts_vol_num = by_key_num["tts_volume"]
        await tts_vol_num.async_set_native_value(99.9)
        self.assertEqual(tts_vol_num.native_value, 5.0)
        await tts_vol_num.async_set_native_value(-3.0)
        self.assertEqual(tts_vol_num.native_value, 0.1)

        hop_cap_num = by_key_num["hopper_capacity_portions"]
        await hop_cap_num.async_set_native_value(2)
        self.assertEqual(hop_cap_num.native_value, 10)
        await hop_cap_num.async_set_native_value(9999)
        self.assertEqual(hop_cap_num.native_value, 500)

    # 16. Coordinator `async_check_bowl` Fallback to `capture_av_clip` When `capture_snapshot` Fails
    async def test_adv_16_check_bowl_falls_back_to_capture_av_clip_when_snapshot_fails(self) -> None:
        coord = self._make_mocked_coordinator()
        await coord.async_set_option(CONF_ENABLE_CAMERA, True)

        coord.client.capture_snapshot = mock.MagicMock(return_value=False)

        def _fallback_av_clip(**kwargs: Any) -> Dict[str, Any]:
            after_p = kwargs.get("after_jpg")
            if after_p:
                Path(after_p).write_bytes(b"\xff\xd8\xff\xe0fallback_jpg")
            return {"video_ok": True}

        coord.client.capture_av_clip = mock.MagicMock(side_effect=_fallback_av_clip)
        fake_vis = mock.MagicMock(
            after_fill_ratio=0.42,
            food_present_cv=True,
            food_increased=False,
            food_present_llm=None,
            dog_at_bowl_detected=False,
            is_infrared_mode=False,
            diagnosis="Fallback bowl check succeeded",
        )
        coord.analyzer.analyze_bowl_visual = mock.MagicMock(return_value=fake_vis)

        res = await coord.async_check_bowl()
        self.assertTrue(res["ok"])
        self.assertEqual(res["outcome"], "bowl_check")
        self.assertEqual(res["bowl_fill_pct"], 42.0)
        self.assertTrue(res["food_present"])
        coord.client.capture_av_clip.assert_called_once()


if __name__ == "__main__":
    unittest.main()
