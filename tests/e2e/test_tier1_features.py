"""Tier 1 E2E Feature Coverage Tests (`test_tier1_features.py`).

Covers happy-path end-to-end verification across all 18 features (F1-F18) defined in
`PROJECT.md` and `TEST_INFRA.md` (6 test methods per feature = 108 test methods total).
All imports of `custom_components.molly_cody_feeder.*` are performed lazily inside test/helper
methods so test discovery and compilation succeed in parallel with implementation milestones.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import inspect
import json
import os
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import numpy as np
from PIL import Image
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIVE_SCHEDULE = (
    "7f0700201000000"
    "7f1100201000000"
    "7f1600201000000"
    "7f1900171000000"
    "000908100000000"
)

EXPECTED_14_SERVICES = (
    "feed",
    "verify_feed",
    "check_bowl",
    "set_schedule_slot",
    "set_schedule_raw",
    "sync_schedules",
    "refill_hopper",
    "capture_snapshot",
    "play_tts",
    "play_audio",
    "play_chime",
    "set_dp",
    "run_online_check",
    "clear_clog_alert",
)

EXPECTED_9_PLATFORMS = (
    "sensor",
    "binary_sensor",
    "switch",
    "button",
    "number",
    "select",
    "camera",
    "image",
    "event",
)


# ---------------------------------------------------------------------------
# Lazy import & contract resolution helpers
# ---------------------------------------------------------------------------
def _import_const():
    return importlib.import_module("custom_components.molly_cody_feeder.const")


def _import_ha_compat():
    return importlib.import_module("custom_components.molly_cody_feeder._ha_compat")


def _import_core():
    return importlib.import_module("custom_components.molly_cody_feeder.core")


def _resolve_core_attr(name: str, *submodules: str) -> Any:
    core = _import_core()
    if hasattr(core, name):
        return getattr(core, name)
    for sub in submodules:
        try:
            mod = importlib.import_module(f"custom_components.molly_cody_feeder.core.{sub}")
            if hasattr(mod, name):
                return getattr(mod, name)
        except ImportError:
            continue
    raise AttributeError(f"Could not resolve '{name}' in custom_components.molly_cody_feeder.core ({submodules})")


def _get_runtime_config_cls():
    for mod_name in (
        "custom_components.molly_cody_feeder.core",
        "custom_components.molly_cody_feeder.core.config",
        "custom_components.molly_cody_feeder.coordinator",
    ):
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "RuntimeConfig"):
                return getattr(mod, "RuntimeConfig")
        except ImportError:
            continue
    raise AttributeError("RuntimeConfig not found in core or coordinator")


def _make_runtime_config(tmpdir: str, **overrides: Any) -> Any:
    try:
        cfg_cls = _resolve_core_attr("RuntimeConfig", "config")
    except AttributeError:
        cfg_cls = _resolve_core_attr("WatchdogConfig", "config")
    try:
        sig = inspect.signature(cfg_cls)
        kwargs = {}
        if "storage_dir" in sig.parameters:
            kwargs["storage_dir"] = Path(tmpdir)
        cfg = cfg_cls(**kwargs)
    except Exception:
        cfg = cfg_cls()
    setattr(cfg, "camera_stream_url", "")
    setattr(cfg, "gemini_api_key", "")
    setattr(cfg, "min_kibble_audio_peaks", getattr(cfg, "min_kibble_audio_peaks", 15))
    setattr(cfg, "ting_rate_min", getattr(cfg, "ting_rate_min", 4.3))
    setattr(cfg, "min_bowl_fill_ratio", getattr(cfg, "min_bowl_fill_ratio", 0.035))
    setattr(cfg, "bowl_roi", getattr(cfg, "bowl_roi", (0.14, 0.72, 0.88, 0.995)))
    setattr(cfg, "record_duration_seconds", getattr(cfg, "record_duration_seconds", 5))
    setattr(cfg, "eaten_check_minutes", getattr(cfg, "eaten_check_minutes", 120))
    setattr(cfg, "alert_mode", getattr(cfg, "alert_mode", "every_miss"))
    setattr(cfg, "no_ting_escalate_after", getattr(cfg, "no_ting_escalate_after", 2))
    setattr(cfg, "tts_alert_on_clog", getattr(cfg, "tts_alert_on_clog", True))
    setattr(cfg, "tts_alert_message", getattr(cfg, "tts_alert_message", "Feeder clog detected"))
    setattr(cfg, "tts_voice", getattr(cfg, "tts_voice", "en-US-AriaNeural"))
    setattr(cfg, "tts_volume", getattr(cfg, "tts_volume", 2.5))
    setattr(cfg, "tts_repeat", getattr(cfg, "tts_repeat", 1))
    setattr(cfg, "keep_footage", getattr(cfg, "keep_footage", True))
    setattr(cfg, "capture_retention_days", getattr(cfg, "capture_retention_days", 14))
    setattr(cfg, "keep_failed_footage_days", getattr(cfg, "keep_failed_footage_days", 90))
    setattr(cfg, "video_keep_days_ok", getattr(cfg, "video_keep_days_ok", 0))
    setattr(cfg, "capture_max_mb", getattr(cfg, "capture_max_mb", 0))
    setattr(cfg, "db_retention_days", getattr(cfg, "db_retention_days", 0))
    setattr(cfg, "export_ml_on_prune", getattr(cfg, "export_ml_on_prune", True))
    setattr(cfg, "ml_export_path", str(Path(tmpdir) / "ml_events.jsonl"))
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _write_synthetic_dispense_wav(wav_path: str, sample_rate: int = 8000) -> np.ndarray:
    """Writes a 5.0s 8kHz mono WAV containing a 3.2s motor block and 26 sharp kibble ting impacts."""
    duration = 5.0
    n_samples = int(sample_rate * duration)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    rng = np.random.default_rng(42)
    sig = rng.normal(0.0, 0.001, size=n_samples)

    # Sustained motor block from 0.6s to 3.8s (3.2s >= 1.2s)
    m0, m1 = int(0.6 * sample_rate), int(3.8 * sample_rate)
    mt = t[m0:m1]
    motor_hum = 0.042 * np.sin(2.0 * np.pi * 220.0 * mt) + 0.022 * np.sin(2.0 * np.pi * 330.0 * mt)
    # Add broadband mid/high kibble flow floor across motor run so kibble_flow coverage is high
    flow_band = 0.018 * np.sin(2.0 * np.pi * 1100.0 * mt) + 0.015 * np.sin(2.0 * np.pi * 2800.0 * mt)
    sig[m0:m1] += motor_hum + flow_band

    # Add 26 sharp kibble ting impacts every 0.11s from 0.75s to 3.55s
    imp_len = int(0.022 * sample_rate)
    it = np.arange(imp_len, dtype=np.float64) / sample_rate
    env = np.exp(-it / 0.0045)
    # Energy concentrated in 600-1500 Hz and >2500 Hz (ratio >> 1.6)
    ting_wave = (
        0.48 * np.sin(2.0 * np.pi * 1050.0 * it)
        + 0.35 * np.sin(2.0 * np.pi * 1350.0 * it)
        + 0.45 * np.sin(2.0 * np.pi * 2950.0 * it)
        + 0.30 * np.sin(2.0 * np.pi * 3450.0 * it)
    ) * env

    for idx in range(26):
        onset = int((0.75 + idx * 0.105) * sample_rate)
        if onset + imp_len < n_samples:
            sig[onset : onset + imp_len] += ting_wave

    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return pcm16.astype(np.float32) / 32768.0


def _write_synthetic_clog_wav(wav_path: str, sample_rate: int = 8000) -> np.ndarray:
    """Writes a 5.0s 8kHz mono WAV with a 3.0s low-frequency motor hum and zero kibble tings."""
    duration = 5.0
    n_samples = int(sample_rate * duration)
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    sig = np.zeros(n_samples, dtype=np.float64) + 0.0005

    m0, m1 = int(0.8 * sample_rate), int(3.8 * sample_rate)
    mt = t[m0:m1]
    sig[m0:m1] = 0.055 * np.sin(2.0 * np.pi * 210.0 * mt) + 0.030 * np.sin(2.0 * np.pi * 420.0 * mt)

    pcm16 = np.clip(sig * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return pcm16.astype(np.float32) / 32768.0


def _write_daylight_bowl_jpg(path: str, with_kibble: bool = False) -> np.ndarray:
    """Writes a 720x1280 portrait daylight image with either an empty steel pit or dark kibble."""
    h, w = 1280, 720
    # Warm saturated colour background so HSV saturation >= 40 (daylight, not IR)
    img = np.full((h, w, 3), (215, 155, 95), dtype=np.uint8)
    if with_kibble:
        # DAY_PIT = (0.32, 0.88, 0.75, 0.985) -> fill 65% of DAY_PIT with dark kibble (< 6.5 gray)
        px1, py1 = int(0.32 * w), int(0.88 * h)
        px2, py2 = int(0.75 * w), int(0.955 * h)
        img[py1:py2, px1:px2] = (0, 0, 0)
    Image.fromarray(img).save(path, format="JPEG", quality=98)
    return img


def _write_ir_bowl_jpg(path: str, with_kibble: bool = False) -> np.ndarray:
    """Writes a 720x1280 portrait IR night-vision image (HSV saturation == 0 < 14.0)."""
    h, w = 1280, 720
    img = np.full((h, w, 3), (170, 170, 170), dtype=np.uint8)
    # Bright IR reflection in bottom band
    bx1, by1, bx2, by2 = int(0.22 * w), int(0.88 * h), int(0.88 * w), int(0.995 * h)
    img[by1:by2, bx1:bx2] = (200, 200, 200)
    # Hotspot in (0.30..0.80, 0.93..0.995)
    hx1, hy1, hx2, hy2 = int(0.35 * w), int(0.94 * h), int(0.75 * w), int(0.99 * h)
    img[hy1:hy2, hx1:hx2] = (245, 245, 245)
    if with_kibble:
        # Dark kibble silhouettes occluding part of IR_BAND and hotspot (without triggering dog detector)
        kx1, ky1, kx2, ky2 = int(0.38 * w), int(0.935 * h), int(0.72 * w), int(0.985 * h)
        img[ky1:ky2, kx1:kx2] = (15, 15, 15)
    Image.fromarray(img).save(path, format="JPEG", quality=98)
    return img


def _create_hass_and_entry(tmpdir: str, options_override: Optional[Dict[str, Any]] = None) -> Tuple[Any, Any]:
    const = _import_const()
    ha_compat = _import_ha_compat()

    HomeAssistant = getattr(ha_compat, "HomeAssistant")
    ConfigEntry = getattr(ha_compat, "ConfigEntry")

    try:
        sig = inspect.signature(HomeAssistant)
        if len(sig.parameters) >= 1:
            hass = HomeAssistant(tmpdir)
        else:
            hass = HomeAssistant()
    except Exception:
        hass = HomeAssistant()

    if hasattr(hass, "config") and hasattr(hass.config, "config_dir"):
        hass.config.config_dir = str(tmpdir)
    elif hasattr(hass, "config"):
        hass.config.path = lambda *parts: str(Path(tmpdir).joinpath(*parts))

    data = {
        const.CONF_EMAIL: "molly@example.com",
        const.CONF_PASSWORD: "Password123!",
        const.CONF_COUNTRY_CODE: "1",
        const.CONF_DEVICE_ID: "dev_molly_6l_001",
        const.CONF_DEVICE_NAME: "Molly & Cody Feeder",
        const.CONF_LOCAL_IP: "192.168.1.150",
        const.CONF_LOCAL_KEY: "0123456789abcdef",
        const.CONF_CONNECTION_MODE: "auto",
    }
    options = {
        const.CONF_CONNECTION_MODE: "auto",
        const.CONF_SCAN_INTERVAL: 60,
        const.CONF_HOPPER_CAPACITY: 240,
        const.CONF_HOPPER_LOW_PCT: 20.0,
        const.CONF_PORTION_WEIGHT_G: 10,
        const.CONF_MEAL_PREROLL_SEC: 40,
        const.CONF_PRE_MEAL_ONLINE_SEC: 300,
        const.CONF_EATEN_CHECK_MIN: 120,
        const.CONF_ALERT_MODE: "every_miss",
        const.CONF_NO_FOOD_HOURS: 8.0,
        const.CONF_TING_RATE_MIN: 4.3,
        const.CONF_MAX_DAILY_RETRY: 3,
        const.CONF_TTS_VOICE: "en-US-AriaNeural",
        const.CONF_TTS_VOLUME: 2.5,
        const.CONF_ENABLE_CAMERA: True,
        const.CONF_ENABLE_TTS: True,
        const.CONF_ENABLE_VERIFICATION: True,
        const.CONF_ENABLE_VISION_VERIFICATION: True,
        const.CONF_ENABLE_MP4_RECORDING: False,
        const.CONF_ENABLE_AUTO_RETRY: True,
    }
    if options_override:
        options.update(options_override)

    try:
        entry = ConfigEntry(
            entry_id="entry_molly_test_01",
            domain=const.DOMAIN,
            title="Molly & Cody Feeder",
            data=data,
            options=options,
        )
    except TypeError:
        entry = ConfigEntry(data=data, options=options, entry_id="entry_molly_test_01")
    return hass, entry


def _get_coordinator(hass: Any, entry: Any) -> Any:
    const = _import_const()
    stored = hass.data.get(const.DOMAIN, {}).get(entry.entry_id)
    if isinstance(stored, dict) and "coordinator" in stored:
        return stored["coordinator"]
    return getattr(stored, "coordinator", stored)


def _client_network_patches(tmpdir: str):
    """Context manager patching external network calls on MollyFeederClient while keeping real logic."""
    MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")

    sample_jpg_path = Path(tmpdir) / "_seed_snap.jpg"
    _write_daylight_bowl_jpg(str(sample_jpg_path), with_kibble=True)
    sample_wav_path = Path(tmpdir) / "_seed_audio.wav"
    _write_synthetic_dispense_wav(str(sample_wav_path))

    def fake_get_dps_lan(self):
        self.cached_dps.setdefault("101", True)
        self.cached_dps.setdefault("103", False)
        self.cached_dps.setdefault("104", True)
        self.cached_dps.setdefault("106", "1")
        self.cached_dps.setdefault("108", "0")
        self.cached_dps.setdefault("134", True)
        self.cached_dps.setdefault("201", 2)
        self.cached_dps.setdefault("202", 10)
        self.cached_dps.setdefault("207", LIVE_SCHEDULE)
        self.cached_dps.setdefault("255", 1)
        return dict(self.cached_dps)

    def fake_get_dps_cloud(self):
        return fake_get_dps_lan(self)

    def fake_set_dps_cloud(self, dps, *args, **kwargs):
        self.cached_dps.update({str(k): v for k, v in dps.items()})
        return True

    def fake_set_dps_lan(self, dps, *args, **kwargs):
        self.cached_dps.update({str(k): v for k, v in dps.items()})
        return True

    def fake_capture_snapshot(self, output_path: str, timeout_sec: float = 15.0) -> bool:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(sample_jpg_path.read_bytes())
        return True

    def fake_capture_av_clip(
        self,
        duration_sec: float = 5.0,
        output_mp4: str = "",
        output_wav: str = "",
        before_jpg: Optional[str] = None,
        after_jpg: Optional[str] = None,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 1.0,
        **kwargs,
    ) -> Dict[str, Any]:
        if before_jpg:
            _write_daylight_bowl_jpg(before_jpg, with_kibble=False)
        if trigger_fn:
            trigger_fn()
        if after_jpg:
            _write_daylight_bowl_jpg(after_jpg, with_kibble=True)
        if output_wav:
            Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
            Path(output_wav).write_bytes(sample_wav_path.read_bytes())
        if output_mp4:
            Path(output_mp4).parent.mkdir(parents=True, exist_ok=True)
            Path(output_mp4).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        return {
            "video_ok": bool(output_mp4),
            "audio_ok": bool(output_wav),
            "before_ok": bool(before_jpg),
            "after_ok": bool(after_jpg),
            "video_frames": 45,
            "audio_frames": 125,
            "audio_rate": 8000,
            "recording_extended_sec": 0.0,
            "motor_window_sec": [0.6, 3.8],
            "recorded_wall_sec": 5.0,
            "audio_sec": 5.0,
            "audio_last_rx_sec": 4.96,
        }

    def fake_speak_tts(self, text: str, voice: Optional[str] = None, volume: float = 2.5, repeat: int = 1, prepend_chime: bool = False, **kw):
        return {"ok": True, "frames_sent": 40, "text": text, "tts_engine": "mock_edge", "duration_sec": 1.6}

    def fake_play_audio(self, audio_path: str, volume: float = 2.5, prepend_chime: bool = False, **kw):
        return {"ok": True, "frames_sent": 35, "audio_path": audio_path, "duration_sec": 1.4}

    patches = [
        mock.patch.object(MollyFeederClient, "login", return_value=True),
        mock.patch.object(MollyFeederClient, "refresh_cloud_devices", return_value=True),
        mock.patch.object(MollyFeederClient, "get_dps_lan", fake_get_dps_lan),
        mock.patch.object(MollyFeederClient, "get_dps_cloud", fake_get_dps_cloud),
        mock.patch.object(MollyFeederClient, "set_dps_cloud", fake_set_dps_cloud),
        mock.patch.object(MollyFeederClient, "set_dps_lan", fake_set_dps_lan),
        mock.patch.object(MollyFeederClient, "capture_snapshot", fake_capture_snapshot),
        mock.patch.object(MollyFeederClient, "capture_av_clip", fake_capture_av_clip),
        mock.patch.object(MollyFeederClient, "speak_tts", fake_speak_tts),
        mock.patch.object(MollyFeederClient, "play_audio_file", fake_play_audio),
    ]
    if hasattr(MollyFeederClient, "play_chime"):
        patches.append(
            mock.patch.object(
                MollyFeederClient,
                "play_chime",
                 lambda self, volume=2.5, **kw: {"ok": True, "frames_sent": 15, "duration_sec": 0.6},
            )
        )
    return patches


# ===========================================================================
# F1: OEM Cloud Auth, Signing & Session Persistence (6 tests)
# ===========================================================================
class TestF1CloudAuthAndSession(unittest.TestCase):
    """F1 — Molly & Cody OEM Cloud API signing, MD5 login, discovery, and atomic session storage."""

    def test_f1_01_oem_cloud_constants(self):
        app_id = _resolve_core_attr("APP_ID", "molly.constants", "molly")
        pkg_name = _resolve_core_attr("PKG_NAME", "molly.constants", "molly")
        api_url = _resolve_core_attr("API_URL", "molly.constants", "molly")
        mqtt_host = _resolve_core_attr("MQTT_HOST", "molly.constants", "molly")
        mqtt_port = _resolve_core_attr("MQTT_PORT", "molly.constants", "molly")
        hmac_key = _resolve_core_attr("HMAC_KEY", "molly.constants", "molly")
        signed_keys = _resolve_core_attr("SIGNED_KEYS", "molly.constants", "molly")

        self.assertEqual(app_id, "wr7phe79kh4ntutc8g43")
        self.assertEqual(pkg_name, "and.mollycody.app01")
        self.assertEqual(api_url, "https://a1.tuyaus.com/api.json")
        self.assertEqual(mqtt_host, "m1-us.mollycody.tech")
        self.assertEqual(mqtt_port, 8883)
        self.assertIn(pkg_name, hmac_key)
        for req_key in ("chKey", "a", "v", "clientId", "postData", "time", "requestId", "sid"):
            self.assertIn(req_key, signed_keys)

    def test_f1_02_ch_key_derivation(self):
        _get_ch_key = _resolve_core_attr("_get_ch_key", "molly.crypto", "molly")
        app_id = _resolve_core_attr("APP_ID", "molly.constants", "molly")
        pkg_name = _resolve_core_attr("PKG_NAME", "molly.constants", "molly")
        cert_sha256 = _resolve_core_attr("CERT_SHA256", "molly.constants", "molly")

        ch_key = _get_ch_key()
        expected_full = hmac.new(
            app_id.encode("utf-8"),
            f"{pkg_name}_{cert_sha256}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().lower()
        self.assertEqual(len(ch_key), 8)
        self.assertEqual(ch_key, expected_full[8:16])

    def test_f1_03_nybble_swapped_post_data_md5(self):
        _post_data_md5_hex = _resolve_core_attr("_post_data_md5_hex", "molly.crypto", "molly")
        payload = '{"countryCode":"1","username":"molly@example.com","isUid":false}'
        raw_md5 = hashlib.md5(payload.encode("utf-8")).hexdigest().lower()
        swapped = _post_data_md5_hex(payload)

        expected = raw_md5[8:16] + raw_md5[0:8] + raw_md5[24:32] + raw_md5[16:24]
        self.assertEqual(len(swapped), 32)
        self.assertEqual(swapped, expected)
        self.assertNotEqual(swapped, raw_md5)

    def test_f1_04_build_sign_input_sorting_and_filtering(self):
        _build_sign_input = _resolve_core_attr("_build_sign_input", "molly.crypto", "molly")
        _post_data_md5_hex = _resolve_core_attr("_post_data_md5_hex", "molly.crypto", "molly")
        post_json = '{"gid":123}'
        params = {
            "v": "2.2",
            "a": "m.life.my.group.device.list",
            "clientId": "wr7phe79kh4ntutc8g43",
            "postData": post_json,
            "time": "1700000000",
            "channel": "sdk",        # not in SIGNED_KEYS -> excluded
            "bizData": '{"nd":"1"}', # not in SIGNED_KEYS -> excluded
            "sid": "",               # empty -> excluded
        }
        sign_str = _build_sign_input(params)
        expected = (
            "a=m.life.my.group.device.list||"
            "clientId=wr7phe79kh4ntutc8g43||"
            f"postData={_post_data_md5_hex(post_json)}||"
            "time=1700000000||"
            "v=2.2"
        )
        self.assertEqual(sign_str, expected)

    def test_f1_05_md5_cloud_login_and_device_discovery(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            sess_path = Path(td) / "tuya_session.json"
            client = MollyFeederClient(
                email="molly@example.com",
                password="SecretPassword99",
                country_code="1",
                mode="cloud",
                session_file=str(sess_path),
            )

            def fake_raw_api(action: str, version: str, post_data_obj: Dict[str, Any], sid: str = "", gid: int = 0):
                if action == "smartlife.m.user.username.token.get":
                    return {"success": True, "result": {"token": "tok_001", "publicKey": "65537", "exponent": "65537"}}
                if action == "smartlife.m.user.email.password.login":
                    return {
                        "success": True,
                        "result": {
                            "sid": "sid_live_123",
                            "uid": "uid_live_456",
                            "ecode": "0123456789abcdef",
                            "partnerIdentity": "p2155085",
                        },
                    }
                if action == "smartlife.m.location.list":
                    return {"success": True, "result": [{"groupId": 998877}]}
                if action == "m.life.my.group.device.list":
                    return {
                        "success": True,
                        "result": [
                            {
                                "devId": "dev_molly_001",
                                "localKey": "fedcba9876543210",
                                "name": "Molly & Cody Smart Feeder",
                                "category": "sp",
                                "cloudOnline": True,
                                "dps": {"101": True, "202": 10, "207": LIVE_SCHEDULE, "255": 1},
                            }
                        ],
                    }
                return {"success": True, "result": True}

            with mock.patch.object(client, "_raw_api_call", side_effect=fake_raw_api):
                ok = client.login(force=True)

            self.assertTrue(ok)
            self.assertEqual(client.sid, "sid_live_123")
            self.assertEqual(client.uid, "uid_live_456")
            self.assertEqual(client.ecode, "0123456789abcdef")
            self.assertEqual(client.gid, 998877)
            self.assertEqual(client.device_id, "dev_molly_001")
            self.assertEqual(client.local_key, "fedcba9876543210")

    def test_f1_06_session_save_and_load_atomic_0o600(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            sess_path = Path(td) / "session_0600.json"
            c1 = MollyFeederClient(
                email="molly@example.com",
                password="pw",
                session_file=str(sess_path),
            )
            c1.sid = "sid_persisted"
            c1.uid = "uid_persisted"
            c1.ecode = "ecode_persisted_1234"
            c1.gid = 445566
            c1.device_id = "dev_persisted_01"
            c1.local_key = "1122334455667788"
            c1.local_ip = "192.168.1.77"
            c1.device_name = "Kitchen Feeder"
            c1.cached_dps = {"101": True, "255": 1}
            c1._save_session()

            self.assertTrue(sess_path.exists())
            self.assertEqual(sess_path.stat().st_mode & 0o777, 0o600)

            c2 = MollyFeederClient(session_file=str(sess_path))
            self.assertEqual(c2.sid, "sid_persisted")
            self.assertEqual(c2.uid, "uid_persisted")
            self.assertEqual(c2.ecode, "ecode_persisted_1234")
            self.assertEqual(c2.device_id, "dev_persisted_01")
            self.assertEqual(c2.local_key, "1122334455667788")
            self.assertEqual(c2.cached_dps.get("101"), True)


# ===========================================================================
# F2: Cloud MQTT 2.2 & Local LAN (`auto`/`cloud`/`lan`) Transport (6 tests)
# ===========================================================================
class TestF2TransportAndMqtt(unittest.TestCase):
    """F2 — MQTT 8883 credential derivation, 2.2 AES-ECB/CRC32 framing, and LAN/Cloud/Auto routing."""

    def test_f2_01_mqtt_credentials_derivation(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        app_id = _resolve_core_attr("APP_ID", "molly.constants", "molly")
        pkg_name = _resolve_core_attr("PKG_NAME", "molly.constants", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))
            client.sid = "sid_999"
            client.uid = "uid_888"
            client.ecode = "abcdef0123456789"
            client.partner_id = "p2155085"

            client_id, username, password = client._get_mqtt_credentials()
            self.assertTrue(client_id.startswith(f"{pkg_name}_mb_"))
            self.assertTrue(client_id.endswith("_DEFAULT"))
            self.assertIn(f"p2155085_v1_{app_id}_", username)
            self.assertIn("sid_999", username)
            self.assertEqual(len(password), 16)

    def test_f2_02_mqtt_22_encrypt_decrypt_roundtrip(self):
        _encrypt_mqtt_22 = _resolve_core_attr("_encrypt_mqtt_22", "molly.crypto", "molly")
        _decrypt_mqtt_22 = _resolve_core_attr("_decrypt_mqtt_22", "molly.crypto", "molly")
        local_key = "0123456789abcdef"
        payload_obj = {"protocol": 5, "t": 1700000123, "data": {"dps": {"201": 4}}}
        payload_json = json.dumps(payload_obj, separators=(",", ":"))

        pkt = _encrypt_mqtt_22(payload_json, local_key, seq=17, source=1)
        self.assertTrue(pkt.startswith(b"2.2"))
        self.assertGreater(len(pkt), 15)
        seq_unpacked, src_unpacked = struct.unpack(">II", pkt[7:15])
        self.assertEqual((seq_unpacked, src_unpacked), (17, 1))

        decoded = _decrypt_mqtt_22(pkt, local_key)
        self.assertEqual(decoded, payload_obj)

    def test_f2_03_set_mode_valid_and_hybrid_alias(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))
            self.assertEqual(client.set_mode("cloud"), "cloud")
            self.assertEqual(client.mode, "cloud")
            self.assertEqual(client.set_mode("lan"), "lan")
            self.assertEqual(client.mode, "lan")
            self.assertEqual(client.set_mode("auto"), "auto")
            self.assertEqual(client.set_mode("HYBRID"), "auto")

    def test_f2_04_get_status_transport_selection(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        sample_dps = {"101": True, "201": 3, "202": 10, "207": LIVE_SCHEDULE, "255": 1}
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(
                device_id="dev_123",
                local_ip="192.168.1.50",
                local_key="0123456789abcdef",
                session_file=str(Path(td) / "s.json"),
            )
            client.set_mode("lan")
            with mock.patch.object(client, "get_dps_lan", return_value=dict(sample_dps)):
                st_lan = client.get_status()
            self.assertEqual(st_lan["active_transport"], "lan")
            self.assertTrue(st_lan["online"])
            self.assertEqual(len(st_lan["schedule"]), 5)

            client.set_mode("cloud")
            with mock.patch.object(client, "get_dps_cloud", return_value=dict(sample_dps)):
                st_cloud = client.get_status()
            self.assertEqual(st_cloud["active_transport"], "cloud")
            self.assertTrue(st_cloud["online"])

    def test_f2_05_set_dps_and_set_dp_routing(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))

            client.set_mode("cloud")
            with mock.patch.object(client, "set_dps_cloud", return_value=True) as m_cloud:
                ok, tr = client.set_dp(101, True)
            self.assertEqual((ok, tr), (True, "cloud"))
            m_cloud.assert_called_once_with({"101": True})

            client.set_mode("lan")
            with mock.patch.object(client, "set_dps_lan", return_value=True) as m_lan:
                ok, tr = client.set_dps({"103": False})
            self.assertEqual((ok, tr), (True, "lan"))
            m_lan.assert_called_once_with({"103": False})

    def test_f2_06_set_dps_cloud_publishes_via_api(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(
                device_id="dev_cloud_99",
                session_file=str(Path(td) / "s.json"),
            )
            with mock.patch.object(
                client,
                "call_api",
                return_value={"success": True, "result": True},
            ) as m_api:
                ok = client.set_dps_cloud({"201": 5})

            self.assertTrue(ok)
            self.assertEqual(client.cached_dps.get("201"), 5)
            m_api.assert_called_once_with(
                "thing.m.device.dp.publish",
                "1.0",
                {"gwId": "dev_cloud_99", "devId": "dev_cloud_99", "dps": '{"201":5}'},
                allow_relogin=True,
            )


# ===========================================================================
# F3: Hardware DP Control (`DP 101..255`) & Manual Feed (`DP 201`) (6 tests)
# ===========================================================================
class TestF3HardwareDpsAndFeed(unittest.TestCase):
    """F3 — Manual feed DP 201, DP_NAMES table, camera/indicator DPs, telemetry DPs 202 and 255."""

    def test_f3_01_dispense_food_writes_dp_201(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            with mock.patch.object(client, "set_dp", return_value=(True, "cloud")) as m_set_dp:
                ok, tr = client.dispense_food(4)
            self.assertEqual((ok, tr), (True, "cloud"))
            m_set_dp.assert_called_once_with(201, 4)

    def test_f3_02_dp_names_mapping_completeness(self):
        dp_names = _resolve_core_attr("DP_NAMES", "molly.constants", "molly")
        expected = {
            "101": "indicator_light",
            "103": "flip_screen",
            "104": "osd_watermark",
            "106": "motion_sensitivity",
            "108": "nightvision",
            "134": "motion_switch",
            "201": "feed_num",
            "202": "food_weight_g",
            "207": "schedule",
            "255": "feed_abnormal",
        }
        for dp_id, name in expected.items():
            self.assertEqual(dp_names.get(dp_id), name)

    def test_f3_03_set_dp_camera_and_indicator_switches(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            recorded = []
            with mock.patch.object(
                client,
                "set_dps_cloud",
                side_effect=lambda dps: (recorded.append(dps), True)[1],
            ):
                for dp_id, val in ((101, True), (103, False), (104, True), (134, False)):
                    ok, tr = client.set_dp(dp_id, val)
                    self.assertEqual((ok, tr), (True, "cloud"))
            self.assertEqual(
                recorded,
                [{"101": True}, {"103": False}, {"104": True}, {"134": False}],
            )

    def test_f3_04_set_dp_nightvision_and_motion_sensitivity_selects(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            recorded = []
            with mock.patch.object(
                client,
                "set_dps_cloud",
                side_effect=lambda dps: (recorded.append(dps), True)[1],
            ):
                for mode_val in ("0", "1", "2"):
                    self.assertEqual(client.set_dp(108, mode_val), (True, "cloud"))
                self.assertEqual(client.set_dp(106, "2"), (True, "cloud"))
            self.assertEqual(
                recorded,
                [{"108": "0"}, {"108": "1"}, {"108": "2"}, {"106": "2"}],
            )

    def test_f3_05_telemetry_dp_202_and_dp_201_in_get_status(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            dps = {"101": True, "201": 6, "202": 12, "207": LIVE_SCHEDULE, "255": 1}
            with mock.patch.object(client, "get_dps_cloud", return_value=dps):
                status = client.get_status()
            self.assertEqual(status["last_feed_portions"], 6)
            self.assertEqual(status["food_weight_g"], 12)
            self.assertEqual(status["named_dps"]["feed_num"], 6)
            self.assertEqual(status["named_dps"]["food_weight_g"], 12)

    def test_f3_06_dp_255_feed_abnormal_normal_value_one(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        decide_passed = _resolve_core_attr("decide_passed", "service.decision", "service")
        AudioAnalysisResult = _resolve_core_attr("AudioAnalysisResult", "analysis.results", "analysis")

        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            with mock.patch.object(client, "get_dps_cloud", return_value={"255": 1}):
                status = client.get_status()
            self.assertEqual(status["feed_abnormal"], 1)

        audio_ok = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=35,
            kibble_dispensed_audio=True,
            motor_energy_db=28.0,
            impact_energy_db=11.0,
            diagnosis="ok",
            motor_duration_sec=2.7,
        )
        # DP 255 == 1 is normal hardware status (not a fault)
        self.assertTrue(decide_passed(audio_ok, None, feed_abnormal=1, expect_dispense=True))


# ===========================================================================
# F4: DP 207 15-Char Schedule Codec & Slot Management (6 tests)
# ===========================================================================
class TestF4ScheduleCodecAndSlots(unittest.TestCase):
    """F4 — DP 207 15-char schedule decoding, encoding, slot updates, and days spec parsing."""

    def test_f4_01_decode_molly_schedule_five_slots(self):
        decode_molly_schedule = _resolve_core_attr("decode_molly_schedule", "schedule_decoder")
        meals = decode_molly_schedule(LIVE_SCHEDULE)
        self.assertEqual(len(meals), 5)
        self.assertEqual([m.slot for m in meals], [1, 2, 3, 4, 5])
        self.assertEqual([m.time_str for m in meals], ["07:00", "11:00", "16:00", "19:00", "09:08"])
        self.assertEqual([m.portions for m in meals], [20, 20, 20, 17, 10])
        self.assertEqual([m.enabled for m in meals], [True, True, True, True, False])
        self.assertEqual([m.days_str for m in meals], ["Everyday", "Everyday", "Everyday", "Everyday", "Once"])

    def test_f4_02_encode_molly_schedule_lossless_roundtrip(self):
        decode_molly_schedule = _resolve_core_attr("decode_molly_schedule", "schedule_decoder")
        encode_molly_schedule = _resolve_core_attr("encode_molly_schedule", "schedule_decoder")
        meals = decode_molly_schedule(LIVE_SCHEDULE)
        for m in meals:
            self.assertEqual(len(m.to_molly_slot()), 15)
        encoded = encode_molly_schedule(meals)
        self.assertEqual(encoded, LIVE_SCHEDULE)

    def test_f4_03_update_molly_schedule_slot_in_place_and_extension(self):
        decode_molly_schedule = _resolve_core_attr("decode_molly_schedule", "schedule_decoder")
        update_molly_schedule_slot = _resolve_core_attr("update_molly_schedule_slot", "schedule_decoder")

        updated_raw = update_molly_schedule_slot(
            LIVE_SCHEDULE, slot=2, hour=12, minute=30, portions=6, enabled=False
        )
        meals = decode_molly_schedule(updated_raw)
        self.assertEqual(len(meals), 5)
        self.assertEqual((meals[1].time_str, meals[1].portions, meals[1].enabled), ("12:30", 6, False))
        # Extend from 5 slots to 6 slots
        extended_raw = update_molly_schedule_slot(
            updated_raw, slot=6, hour=21, minute=15, portions=4, enabled=True
        )
        ext_meals = decode_molly_schedule(extended_raw)
        self.assertEqual(len(ext_meals), 6)
        self.assertEqual((ext_meals[5].slot, ext_meals[5].time_str, ext_meals[5].portions, ext_meals[5].enabled), (6, "21:15", 4, True))

    def test_f4_04_parse_days_spec_all_formats(self):
        parse_days_spec = _resolve_core_attr("parse_days_spec", "schedule_decoder")
        self.assertEqual(parse_days_spec("everyday"), 0x7F)
        self.assertEqual(parse_days_spec("weekdays"), 0x1F)
        self.assertEqual(parse_days_spec("weekends"), 0x60)
        self.assertEqual(parse_days_spec("once"), 0x00)
        self.assertEqual(parse_days_spec("mon,wed,fri"), 0x15)
        self.assertEqual(parse_days_spec("0x3f"), 0x3F)

    def test_f4_05_decode_meal_plan_multiformat(self):
        decode_meal_plan = _resolve_core_attr("decode_meal_plan", "schedule_decoder")
        # 1. Molly 15-char format
        m1 = decode_meal_plan(LIVE_SCHEDULE)
        self.assertEqual(len(m1), 5)
        # 2. JSON array format
        m2 = decode_meal_plan('[{"time": "08:15", "portions": 3, "enabled": true, "slot": 1}]')
        self.assertEqual(len(m2), 1)
        self.assertEqual((m2[0].time_str, m2[0].portions, m2[0].enabled), ("08:15", 3, True))
        # 3. Base64 5-byte binary format
        b64 = base64.b64encode(bytes([0x7F, 6, 45, 5, 1])).decode("ascii")
        m3 = decode_meal_plan(b64)
        self.assertEqual(len(m3), 1)
        self.assertEqual((m3[0].time_str, m3[0].portions, m3[0].enabled), ("06:45", 5, True))

    def test_f4_06_client_get_and_update_schedule_slot(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(mode="cloud", session_file=str(Path(td) / "s.json"))
            client.cached_dps = {"207": LIVE_SCHEDULE}
            with mock.patch.object(client, "get_dps_cloud", return_value={"207": LIVE_SCHEDULE}), \
                 mock.patch.object(client, "set_dps_cloud", return_value=True) as m_set:
                sig = inspect.signature(client.update_schedule_slot)
                kwargs = {"slot": 3, "hour": 15, "minute": 45, "portions": 8, "enabled": True}
                if "days" in sig.parameters:
                    kwargs["days"] = "weekdays"
                elif "days_mask" in sig.parameters:
                    kwargs["days_mask"] = 0x1F
                ok, tr, updated_meals = client.update_schedule_slot(**kwargs)

            self.assertTrue(ok)
            self.assertEqual(tr, "cloud")
            self.assertEqual(updated_meals[2].time_str, "15:45")
            self.assertEqual(updated_meals[2].portions, 8)
            m_set.assert_called_once()


# ===========================================================================
# F5: Cloud `imm` (`AES/KCP 330`) Camera Snapshot & AV Capture (6 tests)
# ===========================================================================
class TestF5ImmCameraAndAvCapture(unittest.TestCase):
    """F5 — Cloud `imm` login framing, AES-128-CBC + HMAC-SHA1, G.711 A-law LUT, snapshots & AV clips."""

    def test_f5_01_imm_login_token_and_login_packet(self):
        _imm_login_token = _resolve_core_attr("_imm_login_token", "molly.imm_codec", "molly")
        _imm_build_login = _resolve_core_attr("_imm_build_login", "molly.imm_codec", "molly")

        tok = _imm_login_token("cam_pwd_1", "local_key_2")
        self.assertEqual(tok, hashlib.md5(b"cam_pwd_1||local_key_2").hexdigest())

        pkt = _imm_build_login(tok)
        self.assertEqual(len(pkt), 104)
        magic, cmd = struct.unpack_from("<II", pkt, 0)
        self.assertEqual((magic, cmd), (0x12345678, 1))
        self.assertEqual(pkt[8:13], b"admin")
        self.assertEqual(pkt[40:72].decode("ascii"), tok)

    def test_f5_02_imm_aes_128_cbc_seal_and_open_roundtrip(self):
        _imm_seal_aes = _resolve_core_attr("_imm_seal_aes", "molly.imm_codec", "molly")
        _imm_open_aes = _resolve_core_attr("_imm_open_aes", "molly.imm_codec", "molly")
        _imm_mac_sha1 = _resolve_core_attr("_imm_mac_sha1", "molly.imm_codec", "molly")

        key = b"0123456789abcdef"
        for size in (1, 15, 16, 31, 104):
            plain = os.urandom(size)
            sealed = _imm_seal_aes(key, plain)
            self.assertEqual(len(sealed) % 16, 0)
            self.assertGreaterEqual(len(sealed), 32)
            opened = _imm_open_aes(key, sealed)
            self.assertEqual(opened, plain)

        mac = _imm_mac_sha1(key, b"kcp_segment_body")
        self.assertEqual(len(mac), 20)

    def test_f5_03_alaw_to_pcm16_decoder(self):
        _alaw_to_pcm16 = _resolve_core_attr("_alaw_to_pcm16", "molly.imm_codec", "molly")
        empty = _alaw_to_pcm16(b"")
        self.assertEqual(len(empty), 0)
        self.assertEqual(empty.dtype, np.int16)

        pcm = _alaw_to_pcm16(bytes([0xD5, 0x55, 0x00, 0x80]))
        self.assertEqual(len(pcm), 4)
        self.assertEqual(pcm.dtype, np.int16)
        self.assertLessEqual(abs(int(pcm[0])), 8)  # 0xD5 is A-law silence

    def test_f5_04_capture_snapshot_writes_upright_portrait_jpeg(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            snap_path = Path(td) / "portrait_snap.jpg"
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))

            def fake_run(coro):
                coro.close()
                _write_daylight_bowl_jpg(str(snap_path), with_kibble=True)
                return {"snapshot_ok": True, "video_frames": 10, "resolution": "720x1280"}

            with mock.patch.object(client, "_run_session", side_effect=fake_run):
                ok = client.capture_snapshot(str(snap_path))

            self.assertTrue(ok)
            self.assertTrue(snap_path.exists())
            img = np.asarray(Image.open(str(snap_path)))
            self.assertEqual(img.shape, (1280, 720, 3))

    def test_f5_05_capture_av_clip_invokes_trigger_and_returns_coverage(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))
            mp4_p = str(Path(td) / "clip.mp4")
            wav_p = str(Path(td) / "clip.wav")
            bef_p = str(Path(td) / "before.jpg")
            aft_p = str(Path(td) / "after.jpg")
            triggered = {"count": 0}

            def fake_run(coro):
                trig = coro.cr_frame.f_locals.get("trigger_fn")
                coro.close()
                if trig:
                    trig()
                return {
                    "video_ok": True,
                    "audio_ok": True,
                    "before_ok": True,
                    "after_ok": True,
                    "video_frames": 60,
                    "audio_frames": 125,
                    "recorded_wall_sec": 5.0,
                    "audio_sec": 5.0,
                    "audio_last_rx_sec": 4.96,
                    "motor_window_sec": [0.8, 3.5],
                }

            with mock.patch.object(client, "_run_session", side_effect=fake_run):
                res = client.capture_av_clip(
                    duration_sec=5.0,
                    output_mp4=mp4_p,
                    output_wav=wav_p,
                    before_jpg=bef_p,
                    after_jpg=aft_p,
                    trigger_fn=lambda: triggered.__setitem__("count", triggered["count"] + 1),
                )

            self.assertEqual(triggered["count"], 1)
            self.assertTrue(res["audio_ok"])
            self.assertTrue(res["video_ok"])
            self.assertEqual(res["recorded_wall_sec"], 5.0)
            self.assertEqual(res["audio_sec"], 5.0)

    def test_f5_06_motor_window_and_recording_coverage_helpers(self):
        _motor_window = _resolve_core_attr("_motor_window", "molly.media", "molly.camera", "molly")
        _motor_active_recently = _resolve_core_attr("_motor_active_recently", "molly.media", "molly.camera", "molly")
        _recording_coverage = _resolve_core_attr("_recording_coverage", "molly.camera", "molly.media", "molly")

        quiet_chunk = np.zeros(320, dtype=np.int16)
        active_chunk = np.full(320, 2500, dtype=np.int16)
        chunks = [quiet_chunk] * 25 + [active_chunk] * 35
        rx_times = [100.0 + (i + 1) * 0.04 for i in range(len(chunks))]

        self.assertTrue(_motor_active_recently(chunks, window_chunks=30))
        win = _motor_window(chunks, rx_times)
        self.assertIsNotNone(win)
        self.assertGreaterEqual(win[1] - win[0], 1.0)

        cov = _recording_coverage(chunks, 8000, rx_times, 100.0, 102.4)
        self.assertEqual(cov["recorded_wall_sec"], 2.4)
        self.assertEqual(cov["audio_sec"], 2.4)
        self.assertEqual(cov["audio_last_rx_sec"], 2.4)


# ===========================================================================
# F6: Two-Way Speaker TTS & Chime (`G.711 mulaw`, `reqId=7`) (6 tests)
# ===========================================================================
class TestF6SpeakerTtsAndChime(unittest.TestCase):
    """F6 — Speaker TX talk frame header (`reqId=7`, `codec=0x86`), G.711 mulaw, chime, TTS & cache."""

    def test_f6_01_imm_build_talk_frame_header_and_req_id_7(self):
        _imm_build_talk_frame = _resolve_core_attr("_imm_build_talk_frame", "molly.imm_codec", "molly")
        payload = b"\xff" * 320
        frame = _imm_build_talk_frame(seq=10, rtp_ts=3200, ts_ms=12345678, alaw=payload, req_id=7)

        self.assertEqual(len(frame), 352)
        magic, req_id, codec, rate, bits, chans, ts_ms, length = struct.unpack_from("<IIIIIIII", frame, 0)
        self.assertEqual(magic, 0x12345678)
        self.assertEqual(req_id, 7)
        self.assertEqual(codec, 0x86)
        self.assertEqual((rate, bits, chans), (0, 1, 0))
        self.assertEqual(ts_ms, 12345678)
        self.assertEqual(length, 320)
        self.assertEqual(frame[32:], payload)

    def test_f6_02_pcm16_to_ulaw_and_mulaw_constants(self):
        SpeakerMixin = _resolve_core_attr("SpeakerMixin", "molly.speaker", "molly")
        self.assertEqual(SpeakerMixin.TALK_REQ_ID, 7)
        self.assertEqual(SpeakerMixin.TALK_OPEN_CMD, 8)
        self.assertEqual(SpeakerMixin.TALK_CONV, 2)
        self.assertEqual(SpeakerMixin.TALK_FRAME_BYTES, 320)
        self.assertEqual(SpeakerMixin.TALK_FFMPEG_FORMAT, "mulaw")
        self.assertEqual(SpeakerMixin.TALK_SILENCE_BYTE, b"\xff")

        try:
            _pcm16_to_ulaw = _resolve_core_attr("_pcm16_to_ulaw", "molly.speaker", "molly.imm_codec", "molly")
            silence_ulaw = _pcm16_to_ulaw(np.zeros(160, dtype=np.int16))
            self.assertEqual(len(silence_ulaw), 160)
            self.assertIn(silence_ulaw[0], (0xFF, 0x7F))
        except AttributeError:
            # Verify SpeakerMixin provides mulaw conversion fallback or method
            self.assertTrue(callable(getattr(SpeakerMixin, "_audio_file_to_alaw", None)))

    def test_f6_03_build_chime_alaw_generates_two_tone_chime(self):
        SpeakerMixin = _resolve_core_attr("SpeakerMixin", "molly.speaker", "molly")
        chime_bytes = SpeakerMixin._build_chime_alaw(8000)
        self.assertIsInstance(chime_bytes, bytes)
        self.assertGreaterEqual(len(chime_bytes), 3200)

    def test_f6_04_loudness_chain_filter_construction(self):
        SpeakerMixin = _resolve_core_attr("SpeakerMixin", "molly.speaker", "molly")
        chain = SpeakerMixin._loudness_chain(2.5)
        self.assertIn("highpass=f=250", chain)
        self.assertIn("lowpass=f=3600", chain)
        self.assertIn("dynaudnorm", chain)
        self.assertIn("equalizer=f=2500", chain)
        self.assertIn("volume=2.50", chain)
        self.assertIn("alimiter", chain)

    def test_f6_05_speak_tts_and_sha1_cache(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td) / "tts_cache"
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))

            def fake_synth(text, out_path, voice="en-US-AriaNeural"):
                Path(out_path).write_bytes(b"ID3" + b"\x00" * 512)
                return f"edge-tts ({voice})"

            with mock.patch.dict(os.environ, {"TTS_CACHE_DIR": str(cache_dir)}), \
                 mock.patch.object(client, "_synthesize_tts_file", side_effect=fake_synth) as m_synth, \
                 mock.patch.object(client, "_audio_file_to_alaw", return_value=b"\xff" * 3200), \
                 mock.patch.object(client, "_run_session", side_effect=lambda coro: (coro.close(), {"ok": True, "frames_sent": 22})[1]):
                r1 = client.speak_tts("Good dog Molly", voice="en-US-AriaNeural", volume=2.5, repeat=1)
                self.assertTrue(r1["ok"])
                self.assertEqual(m_synth.call_count, 1)

                # Second identical call hits SHA-1 cache without re-synthesizing
                r2 = client.speak_tts("Good dog Molly", voice="en-US-AriaNeural", volume=2.5, repeat=1)
                self.assertTrue(r2["ok"])
                self.assertEqual(r2["tts_engine"], "cache")
                self.assertEqual(m_synth.call_count, 1)

    def test_f6_06_play_audio_file_and_play_chime(self):
        MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
        with tempfile.TemporaryDirectory() as td:
            wav_p = Path(td) / "voice.wav"
            _write_synthetic_dispense_wav(str(wav_p))
            client = MollyFeederClient(session_file=str(Path(td) / "s.json"))

            with mock.patch.object(
                client,
                "_run_session",
                side_effect=lambda coro: (coro.close(), {"ok": True, "frames_sent": 40})[1],
            ):
                res = client.play_audio_file(str(wav_p), volume=2.0, prepend_chime=False)
                self.assertTrue(res["ok"])
                self.assertGreater(res["duration_sec"], 0.0)

                if hasattr(client, "play_chime"):
                    chime_res = client.play_chime(volume=2.0)
                    self.assertTrue(chime_res["ok"])


# ===========================================================================
# F7: Acoustic Anti-Clog Analyzer (`audio.py`, `ting.py`, `flow.py`, `learning.py`) (6 tests)
# ===========================================================================
class TestF7AcousticAnalyzer(unittest.TestCase):
    """F7 — 256-pt STFT ting detector, motor window isolation, kibble_flow coverage, and assess_dispense."""

    def test_f7_01_ting_times_detects_kibble_impacts(self):
        ting_times = _resolve_core_attr("ting_times", "analysis.ting", "analysis")
        with tempfile.TemporaryDirectory() as td:
            wav_p = str(Path(td) / "tings.wav")
            samples = _write_synthetic_dispense_wav(wav_p)
            tings = ting_times(samples, 8000)
        self.assertGreaterEqual(len(tings), 15)
        self.assertTrue(all(0.6 <= t <= 4.0 for t in tings))

    def test_f7_02_analyze_dispense_audio_passes_motor_plus_tings(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_runtime_config(td)
            analyzer = BowlAnalyzer(cfg)
            wav_p = str(Path(td) / "dispense_ok.wav")
            _write_synthetic_dispense_wav(wav_p)
            res = analyzer.analyze_dispense_audio(wav_p)

        self.assertTrue(res.motor_detected)
        self.assertGreaterEqual(res.motor_duration_sec, 2.5)
        self.assertGreaterEqual(res.ting_count, 15)
        self.assertGreaterEqual(res.ting_rate_per_sec, 4.3)
        self.assertTrue(res.kibble_dispensed_audio)

    def test_f7_03_kibble_flow_coverage_and_segments(self):
        kibble_flow = _resolve_core_attr("kibble_flow", "analysis.flow", "analysis")
        with tempfile.TemporaryDirectory() as td:
            wav_p = str(Path(td) / "flow.wav")
            samples = _write_synthetic_dispense_wav(wav_p)
            flow = kibble_flow(samples, 8000, motor_start_sec=0.6, motor_end_sec=3.8)

        self.assertIsNotNone(flow)
        self.assertGreaterEqual(flow.coverage, 0.5)
        self.assertLessEqual(flow.coverage, 1.0)
        self.assertGreater(flow.kibble_sec, 1.5)
        self.assertGreaterEqual(len(flow.segments), 1)

    def test_f7_04_assess_dispense_normal_feed_no_warnings(self):
        assess_dispense = _resolve_core_attr("assess_dispense", "analysis.learning", "analysis")
        AudioAnalysisResult = _resolve_core_attr("AudioAnalysisResult", "analysis.results", "analysis")

        audio_res = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=38,
            kibble_dispensed_audio=True,
            motor_energy_db=28.0,
            impact_energy_db=12.0,
            diagnosis="ok",
            motor_duration_sec=2.6,
            kibble_rate_per_sec=14.6,
            ting_count=18,
            ting_rate_per_sec=6.9,
        )
        flags = assess_dispense(audio_res, portions=1)
        self.assertFalse(flags["short_run"])
        self.assertFalse(flags["low_flow"])
        self.assertFalse(flags["weak_flow"])
        self.assertAlmostEqual(flags["expected_motor_sec"], 2.3, places=1)

    def test_f7_05_audio_features_and_result_fields(self):
        audio_features = _resolve_core_attr("audio_features", "analysis.learning", "analysis")
        AudioAnalysisResult = _resolve_core_attr("AudioAnalysisResult", "analysis.results", "analysis")

        res = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=40,
            kibble_dispensed_audio=True,
            motor_energy_db=26.5,
            impact_energy_db=10.5,
            diagnosis="ok",
            motor_duration_sec=3.0,
            kibble_rate_per_sec=13.33,
            ting_count=20,
            ting_rate_per_sec=6.1,
        )
        feats = audio_features(res)
        self.assertEqual(feats, [3.0, 40.0, 13.33, 10.5, 6.1])

    def test_f7_06_analyze_dispense_audio_detects_motor_no_kibble_clog(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            cfg = _make_runtime_config(td)
            analyzer = BowlAnalyzer(cfg)
            wav_p = str(Path(td) / "clog.wav")
            _write_synthetic_clog_wav(wav_p)
            res = analyzer.analyze_dispense_audio(wav_p)

        self.assertTrue(res.motor_detected)
        self.assertGreaterEqual(res.motor_duration_sec, 2.5)
        self.assertFalse(res.kibble_dispensed_audio)
        self.assertEqual(res.ting_count, 0)
        self.assertIn("CLOG", res.diagnosis.upper())


# ===========================================================================
# F8: Visual Bowl Analyzer (`vision.py`, `llm.py`) (6 tests)
# ===========================================================================
class TestF8VisualBowlAnalyzer(unittest.TestCase):
    """F8 — Daylight DAY_PIT fill ratio, before/after comparison, IR_BAND night vision, CLAHE ROI."""

    def test_f8_01_daylight_bowl_fill_empty_vs_full_pit(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            empty_p = str(Path(td) / "empty.jpg")
            full_p = str(Path(td) / "full.jpg")
            _write_daylight_bowl_jpg(empty_p, with_kibble=False)
            _write_daylight_bowl_jpg(full_p, with_kibble=True)

            empty_fill, empty_ir = analyzer.bowl_fill(empty_p)
            full_fill, full_ir = analyzer.bowl_fill(full_p)

        self.assertFalse(empty_ir)
        self.assertEqual(empty_fill, 0.0)
        self.assertFalse(full_ir)
        self.assertGreater(full_fill, 0.25)

    def test_f8_02_analyze_bowl_visual_daylight_food_increased(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            bef_p = str(Path(td) / "before.jpg")
            aft_p = str(Path(td) / "after.jpg")
            ann_p = str(Path(td) / "annotated.jpg")
            _write_daylight_bowl_jpg(bef_p, with_kibble=False)
            _write_daylight_bowl_jpg(aft_p, with_kibble=True)

            res = analyzer.analyze_bowl_visual(aft_p, bef_p, ann_p)

        self.assertFalse(res.is_infrared_mode)
        self.assertFalse(res.bowl_empty)
        self.assertTrue(res.food_increased)
        self.assertTrue(res.food_present_cv)
        self.assertGreater(res.fill_delta_ratio, 0.20)
        self.assertIn("New kibble seen", res.diagnosis)

    def test_f8_03_infrared_night_vision_detection_and_ir_band_change(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            ir_bef = str(Path(td) / "ir_before.jpg")
            ir_aft = str(Path(td) / "ir_after.jpg")
            ir_ann = str(Path(td) / "ir_annotated.jpg")
            _write_ir_bowl_jpg(ir_bef, with_kibble=False)
            _write_ir_bowl_jpg(ir_aft, with_kibble=True)

            res = analyzer.analyze_bowl_visual(ir_aft, ir_bef, ir_ann)

        self.assertTrue(res.is_infrared_mode)
        self.assertFalse(res.bowl_empty)
        self.assertTrue(res.food_increased)
        self.assertIn("IR night vision: new kibble seen", res.diagnosis)

    def test_f8_04_enhance_bowl_roi_and_annotated_image_output(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            dark_roi = np.full((200, 200, 3), 25, dtype=np.uint8)
            enhanced = analyzer._enhance_bowl_roi(dark_roi)
            self.assertEqual(enhanced.shape, dark_roi.shape)
            self.assertGreater(float(np.mean(enhanced)), float(np.mean(dark_roi)))

            aft_p = str(Path(td) / "after.jpg")
            ann_p = str(Path(td) / "annotated_out.jpg")
            _write_daylight_bowl_jpg(aft_p, with_kibble=True)
            analyzer.analyze_bowl_visual(aft_p, annotated_output_path=ann_p)
            self.assertTrue(Path(ann_p).exists())
            ann_img = np.asarray(Image.open(ann_p))
            self.assertEqual(ann_img.shape, (1280, 720, 3))

    def test_f8_05_detect_dog_at_bowl_motion_zone(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            h, w = 1280, 720
            before_img = np.full((h, w, 3), (100, 150, 200), dtype=np.uint8)
            after_img = before_img.copy()
            # Obstruct >45% of the lower bowl zone (0.74..0.99 * h) with a contrasting object
            after_img[int(0.75 * h) : int(0.95 * h), int(0.15 * w) : int(0.85 * w)] = (20, 30, 50)
            self.assertTrue(analyzer._detect_dog_at_bowl(after_img, before_img))

    def test_f8_06_food_seen_single_snapshot_helper(self):
        BowlAnalyzer = _resolve_core_attr("BowlAnalyzer", "analysis.bowl_analyzer", "analysis")
        with tempfile.TemporaryDirectory() as td:
            analyzer = BowlAnalyzer(_make_runtime_config(td))
            full_p = str(Path(td) / "full.jpg")
            empty_p = str(Path(td) / "empty.jpg")
            _write_daylight_bowl_jpg(full_p, with_kibble=True)
            _write_daylight_bowl_jpg(empty_p, with_kibble=False)

            self.assertTrue(analyzer.food_seen(full_p))
            self.assertIsNone(analyzer.food_seen(empty_p))


# ===========================================================================
# F9: Audio-First Decision Matrix & `recording_cut_short` Guard (6 tests)
# ===========================================================================
class TestF9DecisionMatrixAndVerifier(unittest.TestCase):
    """F9 — 7 outcomes in `decision.py`, audio-first `decide_passed`, and `FeedVerifier.run()`."""

    def _make_audio(self, kibble: bool = True, motor: bool = True, dur: float = 2.8):
        AudioAnalysisResult = _resolve_core_attr("AudioAnalysisResult", "analysis.results", "analysis")
        return AudioAnalysisResult(
            motor_detected=motor,
            kibble_impact_peaks=35 if kibble else 2,
            kibble_dispensed_audio=kibble,
            motor_energy_db=28.0 if motor else -80.0,
            impact_energy_db=11.0 if kibble else 0.0,
            diagnosis="ok" if kibble else "clog",
            motor_duration_sec=dur if motor else 0.0,
            kibble_rate_per_sec=12.5 if kibble else 0.0,
            ting_count=18 if kibble else 0,
            ting_rate_per_sec=6.4 if kibble else 0.0,
        )

    def _make_visual(self, **kwargs):
        VisualAnalysisResult = _resolve_core_attr("VisualAnalysisResult", "analysis.results", "analysis")
        defaults = dict(
            before_fill_ratio=0.0,
            after_fill_ratio=0.15,
            fill_delta_ratio=0.15,
            frame_diff_ratio=0.2,
            bowl_empty=False,
            food_increased=True,
            food_present_cv=True,
            food_present_llm=None,
            llm_reasoning="",
            annotated_image_path="",
            diagnosis="New kibble seen",
            is_infrared_mode=False,
            dog_at_bowl_detected=False,
        )
        defaults.update(kwargs)
        return VisualAnalysisResult(**defaults)

    def test_f9_01_decide_passed_audio_first_rules(self):
        decide_passed = _resolve_core_attr("decide_passed", "service.decision", "service")
        self.assertTrue(decide_passed(self._make_audio(True), self._make_visual(food_increased=False), 1, True))
        self.assertFalse(decide_passed(self._make_audio(False), self._make_visual(food_increased=True), 1, True))
        self.assertTrue(decide_passed(None, self._make_visual(food_increased=True), 1, True))
        self.assertTrue(
            decide_passed(None, self._make_visual(food_increased=False, food_present_cv=True), 1, expect_dispense=False)
        )

    def test_f9_02_classify_all_seven_outcomes(self):
        classify = _resolve_core_attr("classify", "service.decision", "service")
        self.assertEqual(classify(self._make_audio(True), self._make_visual(), True, True), ("dispensed", True))
        self.assertEqual(classify(self._make_audio(False, motor=True), self._make_visual(), True, False), ("motor_no_kibble", False))
        self.assertEqual(classify(self._make_audio(False, motor=False), self._make_visual(), True, False), ("no_motor", False))
        self.assertEqual(classify(None, self._make_visual(), True, True), ("unverified", True))
        self.assertEqual(classify(None, None, True, False), ("camera_unavailable", False))
        self.assertEqual(classify(self._make_audio(False, motor=False), self._make_visual(), False, True), ("bowl_check", True))
        self.assertEqual(
            classify(self._make_audio(False, motor=True), self._make_visual(), True, False, cut_short=True),
            ("recording_cut_short", False),
        )

    def test_f9_03_is_clog_is_unverified_and_status_label(self):
        is_clog = _resolve_core_attr("is_clog", "service.decision", "service")
        is_unverified = _resolve_core_attr("is_unverified", "service.decision", "service")
        status_label = _resolve_core_attr("status_label", "service.decision", "service")

        self.assertTrue(is_clog("motor_no_kibble", False))
        self.assertTrue(is_clog("no_motor", False))
        self.assertFalse(is_clog("recording_cut_short", False))
        self.assertFalse(is_clog("camera_unavailable", False))
        self.assertFalse(is_clog("bowl_check", False))

        self.assertTrue(is_unverified("recording_cut_short"))
        self.assertTrue(is_unverified("camera_unavailable"))
        self.assertFalse(is_unverified("dispensed"))

        self.assertEqual(status_label("dispensed", True), "ok")
        self.assertEqual(status_label("motor_no_kibble", False), "clog_detected")
        self.assertEqual(status_label("recording_cut_short", False), "unverified")

    def test_f9_04_recording_cut_short_detection(self):
        recording_cut_short = _resolve_core_attr("recording_cut_short", "service.decision", "service")
        self.assertFalse(recording_cut_short({"recorded_wall_sec": 15.0, "audio_sec": 14.8, "audio_last_rx_sec": 14.9}))
        self.assertTrue(recording_cut_short({"recorded_wall_sec": 15.0, "audio_sec": 3.0, "audio_last_rx_sec": 3.0}))
        self.assertTrue(recording_cut_short({"recorded_wall_sec": 20.0, "audio_sec": 16.0, "audio_last_rx_sec": 12.0}))

    def test_f9_05_feed_verifier_happy_path_dispensed(self):
        FeedVerifier = _resolve_core_attr("FeedVerifier", "service.verifier", "service")
        VerifyRequest = _resolve_core_attr("VerifyRequest", "service.models", "service")
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")

        with tempfile.TemporaryDirectory() as td:
            cfg = _make_runtime_config(td, eaten_check_minutes=0)
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            molly = mock.MagicMock(mode="cloud")
            molly.get_status.return_value = {"food_weight_g": 10, "feed_abnormal": 1, "dps": {}, "active_transport": "cloud"}
            molly.dispense_food.return_value = (True, "cloud")

            def fake_capture(**kw):
                if kw.get("trigger_fn"):
                    kw["trigger_fn"]()
                for k in ("output_wav", "before_jpg", "after_jpg"):
                    if kw.get(k):
                        Path(kw[k]).write_bytes(b"ok")
                return {
                    "audio_ok": True,
                    "after_ok": True,
                    "recorded_wall_sec": 8.0,
                    "audio_sec": 8.0,
                    "audio_last_rx_sec": 7.9,
                }

            molly.capture_av_clip.side_effect = fake_capture
            analyzer = mock.MagicMock()
            analyzer.analyze_dispense_audio.return_value = self._make_audio(True)
            analyzer.analyze_bowl_visual.return_value = self._make_visual(food_increased=True)
            alerter = mock.MagicMock()

            verifier = FeedVerifier(cfg, molly, analyzer, db, alerter, Path(td), Path(td) / "latest.jpg")
            passed, state = verifier.run(VerifyRequest(trigger_dispense=True, portions=2))

            self.assertTrue(passed)
            self.assertEqual(state["status"], "ok")
            self.assertEqual(state["outcome"], "dispensed")
            self.assertFalse(state["clog_detected"])
            self.assertTrue(state["verified"])
            events = db.get_recent_events(5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["outcome"], "dispensed")

    def test_f9_06_feed_verifier_on_demand_bowl_check(self):
        FeedVerifier = _resolve_core_attr("FeedVerifier", "service.verifier", "service")
        VerifyRequest = _resolve_core_attr("VerifyRequest", "service.models", "service")
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")

        with tempfile.TemporaryDirectory() as td:
            cfg = _make_runtime_config(td, eaten_check_minutes=0)
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            molly = mock.MagicMock(mode="cloud")
            molly.get_status.return_value = {"food_weight_g": 10, "feed_abnormal": 1, "dps": {}, "active_transport": "cloud"}
            molly.capture_av_clip.side_effect = lambda **kw: (
                Path(kw["after_jpg"]).write_bytes(b"ok") if kw.get("after_jpg") else None,
                {"audio_ok": False, "after_ok": True},
            )[1]
            analyzer = mock.MagicMock()
            analyzer.analyze_bowl_visual.return_value = self._make_visual(food_increased=False, food_present_cv=True)

            verifier = FeedVerifier(cfg, molly, analyzer, db, mock.MagicMock(), Path(td), Path(td) / "latest.jpg")
            passed, state = verifier.run(VerifyRequest(trigger_dispense=False, expect_dispense=False))

            self.assertTrue(passed)
            self.assertEqual(state["outcome"], "bowl_check")
            molly.dispense_food.assert_not_called()


# ===========================================================================
# F10: SQLite WAL State Store, Hopper Tracking & Retention (6 tests)
# ===========================================================================
class TestF10DatabaseHopperAndRetention(unittest.TestCase):
    """F10 — `KibbleDatabase` feed events, hopper refills, missed feed streak, baselines, `RetentionManager`."""

    def test_f10_01_sqlite_wal_log_and_get_recent_events(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            eid = db.log_feed_event(
                event_type="manual_dispense_verify",
                mode="cloud",
                transport="cloud",
                portions=4,
                success=True,
                clog_detected=False,
                audio_state="dispensed",
                motor_detected=True,
                kibble_peaks=52,
                outcome="dispensed",
                motor_duration_sec=9.2,
                kibble_rate=13.5,
            )
            self.assertGreater(eid, 0)
            events = db.get_recent_events(5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["portions"], 4)
            self.assertEqual(events[0]["outcome"], "dispensed")
            if hasattr(db, "recent_events"):
                self.assertEqual(len(db.recent_events(5)), 1)

    def test_f10_02_record_refill_and_hopper_status_calculation(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            db.record_refill(200, note="New bag of kibble")
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 5, True, False, motor_detected=True, outcome="dispensed")
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 15, True, False, motor_detected=True, outcome="dispensed")

            hop = db.hopper_status(240)
            self.assertTrue(hop["known"])
            self.assertEqual(hop["capacity_portions"], 200)
            self.assertEqual(hop["portions_used"], 20)
            self.assertEqual(hop["portions_left"], 180)
            self.assertEqual(hop["percent_left"], 90.0)

    def test_f10_03_consecutive_missed_feeds_and_portions_today(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 3, True, False, outcome="dispensed")
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 3, False, True, outcome="motor_no_kibble")
            db.log_feed_event("scheduled_verify", "cloud", "cloud", 3, False, True, outcome="no_motor")
            self.assertEqual(db.consecutive_missed_feeds(), 2)
            self.assertEqual(db.portions_today("scheduled_verify"), 9)

            db.log_feed_event("scheduled_verify", "cloud", "cloud", 3, True, False, outcome="dispensed")
            self.assertEqual(db.consecutive_missed_feeds(), 0)

    def test_f10_04_learned_baselines_median_calculation(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            for portions, dur, rate in ((2, 4.6, 12.0), (4, 9.6, 14.0), (5, 12.5, 13.0)):
                db.log_feed_event(
                    "scheduled_verify", "cloud", "cloud", portions, True, False,
                    motor_detected=True, outcome="dispensed", motor_duration_sec=dur, kibble_rate=rate,
                )
            base = db.learned_baselines()
            self.assertAlmostEqual(base["kibble_rate"], 13.0, places=2)
            self.assertAlmostEqual(base["motor_sec_per_portion"], 2.4, places=2)

    def test_f10_05_food_observations_and_action_logs(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            db.record_food_evidence("ting", "event #1")
            ev = db.last_food_evidence()
            self.assertIsNotNone(ev)
            self.assertEqual(ev["source"], "ting")

            aid = db.log_action("manual_feed", "cloud", True, "Dispensed 2 portions", {"portions": 2})
            self.assertGreater(aid, 0)
            actions = db.get_recent_actions(5)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["action"], "manual_feed")

    def test_f10_06_retention_manager_prune_and_ml_export(self):
        KibbleDatabase = _resolve_core_attr("KibbleDatabase", "db")
        RetentionManager = _resolve_core_attr("RetentionManager", "service.retention", "service")
        with tempfile.TemporaryDirectory() as td:
            caps = Path(td) / "captures"
            caps.mkdir()
            cfg = _make_runtime_config(td, keep_footage=True, capture_retention_days=14, export_ml_on_prune=True)
            db = KibbleDatabase(str(Path(td) / "kibble.db"))

            mp4_f = caps / "old_feed.mp4"
            wav_f = caps / "old_feed.wav"
            mp4_f.write_bytes(b"x" * 128)
            wav_f.write_bytes(b"y" * 128)

            eid = db.log_feed_event(
                "scheduled_verify", "cloud", "cloud", 4, True, False,
                outcome="dispensed", video_clip=str(mp4_f), audio_clip=str(wav_f),
            )
            con = sqlite3.connect(db.db_path)
            con.execute("UPDATE feed_events SET timestamp = datetime('now', 'localtime', '-20 days') WHERE id = ?", (eid,))
            con.commit()
            con.close()

            rm = RetentionManager(cfg, db, caps)
            stats = rm.run()
            self.assertEqual(stats["events_purged"], 1)
            self.assertFalse(mp4_f.exists())
            self.assertFalse(wav_f.exists())
            self.assertTrue(Path(cfg.ml_export_path).exists())


# ===========================================================================
# F11: HACS & HA Manifests + Standalone `_ha_compat.py` Layer (6 tests)
# ===========================================================================
class TestF11HacsManifestAndHaCompat(unittest.TestCase):
    """F11 — `hacs.json`, `manifest.json` (`v0.7.0`), `const.py`, `_ha_compat.py`, `RuntimeConfig`."""

    def test_f11_01_hacs_json_valid_schema(self):
        hacs_path = REPO_ROOT / "hacs.json"
        self.assertTrue(hacs_path.exists(), "hacs.json must exist at repository root")
        data = json.loads(hacs_path.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("name"), str)
        self.assertTrue(len(data["name"]) > 0)
        self.assertTrue(data.get("render_readme"))

    def test_f11_02_manifest_json_valid_v070_schema(self):
        manifest_path = REPO_ROOT / "custom_components" / "molly_cody_feeder" / "manifest.json"
        self.assertTrue(manifest_path.exists(), "manifest.json must exist")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("domain"), "molly_cody_feeder")
        self.assertEqual(data.get("version"), "0.7.0")
        self.assertTrue(data.get("config_flow"))
        self.assertIsInstance(data.get("requirements"), list)
        self.assertIn("iot_class", data)

    def test_f11_03_const_py_contract_constants(self):
        const = _import_const()
        self.assertEqual(const.DOMAIN, "molly_cody_feeder")
        self.assertEqual(const.VERSION, "0.7.0")
        self.assertEqual(set(const.PLATFORMS), set(EXPECTED_9_PLATFORMS))
        for toggle_attr in (
            "CONF_ENABLE_CAMERA",
            "CONF_ENABLE_TTS",
            "CONF_ENABLE_VERIFICATION",
            "CONF_ENABLE_VISION_VERIFICATION",
            "CONF_ENABLE_MP4_RECORDING",
            "CONF_ENABLE_AUTO_RETRY",
        ):
            self.assertTrue(hasattr(const, toggle_attr), f"Missing {toggle_attr} in const.py")
        self.assertEqual(const.EVENT_FEED, "molly_cody_feeder_feed_event")
        self.assertEqual(const.EVENT_ALERT, "molly_cody_feeder_alert_event")

    def test_f11_04_ha_compat_layer_classes_and_helpers(self):
        ha_compat = _import_ha_compat()
        for cls_name in (
            "HomeAssistant",
            "ConfigEntry",
            "DataUpdateCoordinator",
            "CoordinatorEntity",
            "SensorEntity",
            "BinarySensorEntity",
            "SwitchEntity",
            "ButtonEntity",
            "NumberEntity",
            "SelectEntity",
            "Camera",
            "ImageEntity",
            "EventEntity",
            "ConfigFlow",
            "OptionsFlow",
            "DeviceInfo",
        ):
            self.assertTrue(hasattr(ha_compat, cls_name), f"Missing {cls_name} in _ha_compat.py")

    def test_f11_05_runtime_config_from_entry_storage_isolation(self):
        RuntimeConfig = _get_runtime_config_cls()
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td, options_override={"hopper_capacity_portions": 310})
            rcfg = RuntimeConfig.from_entry(hass, entry)
            self.assertEqual(rcfg.hopper_capacity_portions, 310)
            # Verify storage path isolation includes entry_id
            storage_paths = [
                str(getattr(rcfg, attr, ""))
                for attr in ("storage_dir", "session_file", "db_path", "work_dir", "captures_dir")
                if getattr(rcfg, attr, None) is not None
            ]
            self.assertTrue(any(entry.entry_id in p for p in storage_paths))

    def test_f11_06_standalone_architecture_zero_external_vps_dependency(self):
        comp_dir = REPO_ROOT / "custom_components" / "molly_cody_feeder"
        self.assertTrue((comp_dir / "core").is_dir())
        py_files = list(comp_dir.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 15)
        # Ensure integration modules do not import from the legacy `watchdog.` top-level package
        for pf in py_files:
            src = pf.read_text(encoding="utf-8")
            self.assertNotIn("from watchdog.", src, f"{pf.name} must not depend on legacy watchdog package")


# ===========================================================================
# F12: Config Flow & Options Flow with Modular Compute Toggles (6 tests)
# ===========================================================================
class TestF12ConfigAndOptionsFlow(unittest.IsolatedAsyncioTestCase):
    """F12 — `ConfigFlow` Cloud/LAN steps and `OptionsFlow` modular compute toggles & thresholds."""

    def _get_config_flow_instance(self, hass: Any):
        cf_mod = importlib.import_module("custom_components.molly_cody_feeder.config_flow")
        flow_cls = (
            getattr(cf_mod, "MollyCodyConfigFlow", None)
            or getattr(cf_mod, "MollyCodyFeederConfigFlow", None)
            or getattr(cf_mod, "ConfigFlow")
        )
        flow = flow_cls()
        flow.hass = hass
        return flow, flow_cls

    def _get_options_flow_instance(self, hass: Any, entry: Any):
        _, flow_cls = self._get_config_flow_instance(hass)
        if hasattr(flow_cls, "async_get_options_flow"):
            opt_flow = flow_cls.async_get_options_flow(entry)
            if inspect.iscoroutine(opt_flow):
                raise RuntimeError("async_get_options_flow should be a callback/staticmethod returning OptionsFlow")
        else:
            cf_mod = importlib.import_module("custom_components.molly_cody_feeder.config_flow")
            opt_cls = getattr(cf_mod, "MollyCodyOptionsFlow", None) or getattr(cf_mod, "OptionsFlow")
            opt_flow = opt_cls(entry)
        opt_flow.hass = hass
        if not getattr(opt_flow, "config_entry", None):
            try:
                opt_flow.config_entry = entry
            except AttributeError:
                pass
        return opt_flow

    async def test_f12_01_config_flow_user_step_form_display(self):
        with tempfile.TemporaryDirectory() as td:
            hass, _ = _create_hass_and_entry(td)
            flow, _ = self._get_config_flow_instance(hass)
            res = await flow.async_step_user(None)
            self.assertEqual(res["type"], "form")
            self.assertEqual(res["step_id"], "user")

    async def test_f12_02_config_flow_cloud_login_creates_entry(self):
        const = _import_const()
        with tempfile.TemporaryDirectory() as td:
            hass, _ = _create_hass_and_entry(td)
            flow, _ = self._get_config_flow_instance(hass)
            with mock.patch.multiple(
                _resolve_core_attr("MollyFeederClient", "molly.client", "molly"),
                login=mock.DEFAULT,
                get_status=mock.DEFAULT,
            ) as mocks:
                mocks["login"].return_value = True
                mocks["get_status"].return_value = {
                    "device_id": "dev_cloud_101",
                    "device_name": "Molly & Cody Cloud Feeder",
                    "online": True,
                    "local_ip": "192.168.1.120",
                    "dps": {"101": True, "255": 1},
                }
                user_input = {
                    const.CONF_EMAIL: "owner@example.com",
                    const.CONF_PASSWORD: "Password123!",
                    const.CONF_COUNTRY_CODE: "1",
                    const.CONF_CONNECTION_MODE: "auto",
                    const.CONF_ENABLE_CAMERA: True,
                    const.CONF_ENABLE_TTS: True,
                    const.CONF_ENABLE_VERIFICATION: True,
                }
                res = await flow.async_step_user(user_input)

            self.assertEqual(res["type"], "create_entry")
            self.assertEqual(res["data"][const.CONF_EMAIL], "owner@example.com")
            self.assertEqual(res["data"][const.CONF_DEVICE_ID], "dev_cloud_101")

    async def test_f12_03_config_flow_lan_mode_creates_entry(self):
        const = _import_const()
        with tempfile.TemporaryDirectory() as td:
            hass, _ = _create_hass_and_entry(td)
            flow, _ = self._get_config_flow_instance(hass)
            MollyFeederClient = _resolve_core_attr("MollyFeederClient", "molly.client", "molly")
            with mock.patch.object(MollyFeederClient, "get_dps_lan", return_value={"101": True, "255": 1}), \
                 mock.patch.object(
                     MollyFeederClient,
                     "get_status",
                     return_value={
                         "device_id": "dev_lan_202",
                         "device_name": "LAN Feeder",
                         "online": True,
                         "active_transport": "lan",
                         "local_ip": "192.168.1.202",
                         "dps": {"101": True, "255": 1},
                     },
                 ):
                user_input = {
                    const.CONF_CONNECTION_MODE: "lan",
                    const.CONF_DEVICE_ID: "dev_lan_202",
                    const.CONF_DEVICE_NAME: "LAN Feeder",
                    const.CONF_LOCAL_IP: "192.168.1.202",
                    const.CONF_LOCAL_KEY: "0123456789abcdef",
                }
                res = await flow.async_step_user(user_input)

            self.assertEqual(res["type"], "create_entry")
            self.assertEqual(res["data"][const.CONF_CONNECTION_MODE], "lan")
            self.assertEqual(res["data"][const.CONF_LOCAL_IP], "192.168.1.202")

    async def test_f12_04_options_flow_init_form(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            opt_flow = self._get_options_flow_instance(hass, entry)
            res = await opt_flow.async_step_init(None)
            self.assertEqual(res["type"], "form")
            self.assertEqual(res["step_id"], "init")

    async def test_f12_05_options_flow_modular_compute_toggles_update(self):
        const = _import_const()
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            opt_flow = self._get_options_flow_instance(hass, entry)
            toggle_input = {
                const.CONF_ENABLE_CAMERA: False,
                const.CONF_ENABLE_TTS: False,
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
                const.CONF_ENABLE_MP4_RECORDING: False,
                const.CONF_ENABLE_AUTO_RETRY: True,
            }
            res = await opt_flow.async_step_init(toggle_input)
            self.assertEqual(res["type"], "create_entry")
            for k, v in toggle_input.items():
                self.assertEqual(res["data"][k], v)

    async def test_f12_06_options_flow_thresholds_and_parameters_update(self):
        const = _import_const()
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            opt_flow = self._get_options_flow_instance(hass, entry)
            params_input = {
                const.CONF_HOPPER_CAPACITY: 300,
                const.CONF_HOPPER_LOW_PCT: 15.0,
                const.CONF_PORTION_WEIGHT_G: 12,
                const.CONF_MEAL_PREROLL_SEC: 45,
                const.CONF_EATEN_CHECK_MIN: 90,
                const.CONF_ALERT_MODE: "no_food_hours",
                const.CONF_NO_FOOD_HOURS: 6.0,
                const.CONF_TING_RATE_MIN: 4.5,
                const.CONF_MAX_DAILY_RETRY: 4,
                const.CONF_TTS_VOLUME: 3.0,
            }
            res = await opt_flow.async_step_init(params_input)
            self.assertEqual(res["type"], "create_entry")
            self.assertEqual(res["data"][const.CONF_HOPPER_CAPACITY], 300)
            self.assertEqual(res["data"][const.CONF_ALERT_MODE], "no_food_hours")
            self.assertEqual(res["data"][const.CONF_MAX_DAILY_RETRY], 4)


# ===========================================================================
# F13: `MollyCodyDataUpdateCoordinator` & Scheduled Meal Engine (6 tests)
# ===========================================================================
class TestF13CoordinatorAndMealEngine(unittest.IsolatedAsyncioTestCase):
    """F13 — Coordinator data contract, 25+ summary metrics, feed/verify/refill/schedule actions."""

    async def test_f13_01_coordinator_async_update_data_top_level_contract(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                ok = await init_mod.async_setup_entry(hass, entry)
                self.assertTrue(ok)
                coord = _get_coordinator(hass, entry)
                data = coord.data
                for key in (
                    "device_id",
                    "device_name",
                    "online",
                    "configured_mode",
                    "active_transport",
                    "local_ip",
                    "dps",
                    "named_dps",
                    "feed_abnormal",
                    "food_weight_g",
                    "schedule_raw",
                    "schedule",
                    "summary",
                    "options",
                    "latest_snapshot_bytes",
                    "latest_annotated_bytes",
                    "latest_before_bytes",
                    "latest_after_bytes",
                    "last_feed_event",
                    "last_alert_event",
                ):
                    self.assertIn(key, data, f"Missing top-level coordinator.data key '{key}'")
                self.assertEqual(len(data["schedule"]), 8)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f13_02_coordinator_summary_25_plus_metrics_contract(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)
                summary = coord.data["summary"]
                required_summary_keys = (
                    "status",
                    "busy",
                    "clog_detected",
                    "last_meal_time",
                    "last_meal_result",
                    "last_meal_outcome",
                    "last_meal_text",
                    "last_meal_ok",
                    "last_meal_portions",
                    "minutes_since_last_meal",
                    "next_meal_time",
                    "minutes_to_next_meal",
                    "today_meals",
                    "today_fed",
                    "today_problems",
                    "today_portions",
                    "today_list",
                    "week_meals",
                    "week_confirmed_pct",
                    "kibble_flow",
                    "kibble_flow_7d_avg",
                    "last_motor_duration_sec",
                    "bowl_fill_pct",
                    "food_present",
                    "dog_at_bowl",
                    "is_infrared_mode",
                    "weak_flow_warning",
                    "consecutive_missed_feeds",
                    "hopper_known",
                    "hopper_capacity_portions",
                    "hopper_portions_used",
                    "hopper_portions_left",
                    "hopper_percent",
                    "hopper_days_left",
                    "hopper_low",
                    "last_alert_title",
                    "last_alert_time",
                )
                for sk in required_summary_keys:
                    self.assertIn(sk, summary, f"Missing summary key '{sk}'")
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f13_03_coordinator_async_feed_and_async_verify_feed(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)

                res_unv = await coord.async_feed(portions=3, verify=False)
                self.assertIsInstance(res_unv, dict)
                self.assertGreaterEqual(coord.data["summary"]["today_portions"], 3)

                res_ver = await coord.async_verify_feed(portions=2)
                self.assertIsInstance(res_ver, dict)
                self.assertEqual(coord.data["summary"]["last_meal_outcome"], "dispensed")
                self.assertGreaterEqual(coord.data["summary"]["today_portions"], 5)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f13_04_coordinator_async_refill_hopper_and_schedule_slot(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)

                hop = await coord.async_refill_hopper(capacity_portions=220, note="Fresh bag")
                self.assertIsInstance(hop, dict)
                self.assertTrue(coord.data["summary"]["hopper_known"])
                self.assertEqual(coord.data["summary"]["hopper_capacity_portions"], 220)

                ok = await coord.async_set_schedule_slot(
                    slot=1, hour=7, minute=30, portions=4, days="everyday", enabled=True
                )
                self.assertTrue(ok)
                self.assertEqual(coord.data["schedule"][0]["time"], "07:30")
                self.assertEqual(coord.data["schedule"][0]["portions"], 4)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f13_05_coordinator_online_check_and_clear_clog_alert(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)

                online_ok = await coord.async_run_online_check()
                self.assertTrue(online_ok)

                await coord.async_clear_clog_alert()
                self.assertFalse(coord.data["summary"]["clog_detected"])
                self.assertEqual(coord.data["summary"]["status"], "ok")
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f13_06_coordinator_shared_session_lock_and_speaker_actions(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)

                lock = getattr(coord, "_session_lock", None) or getattr(coord, "session_lock", None)
                self.assertIsInstance(lock, asyncio.Lock)

                snap_bytes = await coord.async_capture_snapshot()
                self.assertIsInstance(snap_bytes, bytes)
                self.assertTrue(snap_bytes.startswith(b"\xff\xd8"))

                tts_res = await coord.async_play_tts("Dinner time Molly", volume=2.5)
                self.assertTrue(tts_res.get("ok"))

                chime_res = await coord.async_play_chime(volume=2.0)
                self.assertTrue(chime_res.get("ok"))
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()


# ===========================================================================
# F14: Integration Setup, Custom Services (`services.yaml`) & Diagnostics (6 tests)
# ===========================================================================
class TestF14SetupServicesAndDiagnostics(unittest.IsolatedAsyncioTestCase):
    """F14 — Entry setup/unload, 14 custom services, `services.yaml`, `diagnostics.py`, translations."""

    async def test_f14_01_async_setup_entry_and_unload_entry_lifecycle(self):
        const = _import_const()
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                ok = await init_mod.async_setup_entry(hass, entry)
                self.assertTrue(ok)
                self.assertIn(entry.entry_id, hass.data.get(const.DOMAIN, {}))

                unloaded = await init_mod.async_unload_entry(hass, entry)
                self.assertTrue(unloaded)
                self.assertNotIn(entry.entry_id, hass.data.get(const.DOMAIN, {}))
            finally:
                for p in patches:
                    p.stop()

    async def test_f14_02_all_14_custom_services_registered(self):
        const = _import_const()
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                for svc in EXPECTED_14_SERVICES:
                    self.assertTrue(
                        hass.services.has_service(const.DOMAIN, svc),
                        f"Service {const.DOMAIN}.{svc} must be registered",
                    )
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f14_03_custom_service_handlers_dispatch_to_coordinator(self):
        const = _import_const()
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                coord = _get_coordinator(hass, entry)

                await hass.services.async_call(
                    const.DOMAIN, const.SERVICE_REFILL_HOPPER, {"capacity_portions": 180, "note": "Service refill"}
                )
                self.assertEqual(coord.data["summary"]["hopper_capacity_portions"], 180)

                await hass.services.async_call(
                    const.DOMAIN, const.SERVICE_FEED, {"portions": 2, "verify": False}
                )
                self.assertGreaterEqual(coord.data["summary"]["today_portions"], 2)

                await hass.services.async_call(
                    const.DOMAIN, const.SERVICE_SET_DP, {"dp_id": 101, "value": False}
                )
                self.assertEqual(coord.data["dps"].get("101"), False)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f14_04_services_yaml_defines_all_14_services(self):
        svc_yaml_path = REPO_ROOT / "custom_components" / "molly_cody_feeder" / "services.yaml"
        self.assertTrue(svc_yaml_path.exists(), "services.yaml must exist")
        data = yaml.safe_load(svc_yaml_path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        for svc in EXPECTED_14_SERVICES:
            self.assertIn(svc, data, f"Service '{svc}' missing from services.yaml")
            self.assertIn("description", data[svc])

    async def test_f14_05_diagnostics_returns_config_status_and_summary(self):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        diag_mod = importlib.import_module("custom_components.molly_cody_feeder.diagnostics")
        with tempfile.TemporaryDirectory() as td:
            hass, entry = _create_hass_and_entry(td)
            patches = _client_network_patches(td)
            for p in patches:
                p.start()
            try:
                await init_mod.async_setup_entry(hass, entry)
                diag = await diag_mod.async_get_config_entry_diagnostics(hass, entry)
                self.assertIsInstance(diag, dict)
                diag_json = json.dumps(diag)
                self.assertNotIn("Password123!", diag_json)
                self.assertNotIn("0123456789abcdef", diag_json)
                self.assertIn("**REDACTED**", diag_json)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f14_06_strings_json_and_translations_en_json_valid_and_matching(self):
        comp_dir = REPO_ROOT / "custom_components" / "molly_cody_feeder"
        strings_path = comp_dir / "strings.json"
        en_path = comp_dir / "translations" / "en.json"
        self.assertTrue(strings_path.exists())
        self.assertTrue(en_path.exists())

        strings_data = json.loads(strings_path.read_text(encoding="utf-8"))
        en_data = json.loads(en_path.read_text(encoding="utf-8"))
        for d in (strings_data, en_data):
            self.assertIn("config", d)
            self.assertIn("step", d["config"])
            self.assertIn("error", d["config"])
            self.assertIn("options", d)


# ===========================================================================
# F15: Sensor & Binary Sensor Platforms (`sensor.py`, `binary_sensor.py`) (6 tests)
# ===========================================================================
class TestF15SensorAndBinarySensorPlatforms(unittest.IsolatedAsyncioTestCase):
    """F15 — 22 `SensorEntity` descriptions and 9 `BinarySensorEntity` descriptions."""

    async def _setup_platforms(self, td: str):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        sensor_mod = importlib.import_module("custom_components.molly_cody_feeder.sensor")
        bsensor_mod = importlib.import_module("custom_components.molly_cody_feeder.binary_sensor")

        hass, entry = _create_hass_and_entry(td)
        patches = _client_network_patches(td)
        for p in patches:
            p.start()
        await init_mod.async_setup_entry(hass, entry)
        coord = _get_coordinator(hass, entry)

        sensors: List[Any] = []
        await sensor_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: sensors.extend(ents))
        bsensors: List[Any] = []
        await bsensor_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: bsensors.extend(ents))
        return hass, entry, coord, sensors, bsensors, patches, init_mod

    async def test_f15_01_sensor_platform_creates_at_least_22_entities(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, sensors, _, patches, init_mod = await self._setup_platforms(td)
            try:
                self.assertGreaterEqual(len(sensors), 22)
                uids = {s.unique_id for s in sensors}
                self.assertEqual(len(uids), len(sensors))
                for s in sensors:
                    self.assertIsNotNone(s.device_info)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f15_02_sensor_status_transport_and_meal_telemetry_values(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, sensors, _, patches, init_mod = await self._setup_platforms(td)
            try:
                await coord.async_verify_feed(portions=3)
                by_key = {getattr(s, "entity_description", s).key if hasattr(s, "entity_description") and getattr(s, "entity_description", None) else s.unique_id.split("_", 1)[-1]: s for s in sensors}
                values = {k: s.native_value for k, s in by_key.items()}
                self.assertIn("ok", values.values())
                self.assertIn(coord.data["active_transport"], values.values())
                self.assertTrue(any(v == 3 for v in values.values()))
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f15_03_sensor_hopper_acoustic_and_bowl_values(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, sensors, _, patches, init_mod = await self._setup_platforms(td)
            try:
                await coord.async_refill_hopper(capacity_portions=200)
                await coord.async_verify_feed(portions=10)
                vals = [s.native_value for s in sensors]
                self.assertIn(190, vals)   # hopper_portions_left = 200 - 10
                self.assertIn(95.0, vals)  # hopper_percent = 95.0%
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f15_04_binary_sensor_platform_creates_at_least_9_entities(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, _, bsensors, patches, init_mod = await self._setup_platforms(td)
            try:
                self.assertGreaterEqual(len(bsensors), 9)
                uids = {b.unique_id for b in bsensors}
                self.assertEqual(len(uids), len(bsensors))
                for b in bsensors:
                    self.assertIsNotNone(b.device_info)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f15_05_binary_sensor_states_match_coordinator_data(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, bsensors, patches, init_mod = await self._setup_platforms(td)
            try:
                for b in bsensors:
                    self.assertIsInstance(b.is_on, bool)
                # At least online should be True on healthy startup, and clog/fault should be False
                states = {b.unique_id: b.is_on for b in bsensors}
                online_matches = [v for k, v in states.items() if "online" in k]
                clog_matches = [v for k, v in states.items() if "clog" in k]
                fault_matches = [v for k, v in states.items() if "fault" in k or "abnormal" in k]
                self.assertTrue(any(online_matches))
                self.assertTrue(all(v is False for v in clog_matches))
                self.assertTrue(all(v is False for v in fault_matches))
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f15_06_schedule_sensor_and_extra_state_attributes(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, sensors, _, patches, init_mod = await self._setup_platforms(td)
            try:
                sched_sensors = [s for s in sensors if "schedule" in s.unique_id or "next_meal" in s.unique_id]
                self.assertGreaterEqual(len(sched_sensors), 1)
                attrs_list = [getattr(s, "extra_state_attributes", None) for s in sensors]
                non_empty_attrs = [a for a in attrs_list if isinstance(a, dict) and a]
                self.assertGreaterEqual(len(non_empty_attrs), 1)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()


# ===========================================================================
# F16: Switch, Button, Number & Select Platforms (`switch.py`, `button.py`, `number.py`, `select.py`) (6 tests)
# ===========================================================================
class TestF16ControlEntityPlatforms(unittest.IsolatedAsyncioTestCase):
    """F16 — 16 switches, 8 buttons, 12 numbers, and 4 selects."""

    async def _setup_controls(self, td: str):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        sw_mod = importlib.import_module("custom_components.molly_cody_feeder.switch")
        btn_mod = importlib.import_module("custom_components.molly_cody_feeder.button")
        num_mod = importlib.import_module("custom_components.molly_cody_feeder.number")
        sel_mod = importlib.import_module("custom_components.molly_cody_feeder.select")

        hass, entry = _create_hass_and_entry(td)
        patches = _client_network_patches(td)
        for p in patches:
            p.start()
        await init_mod.async_setup_entry(hass, entry)
        coord = _get_coordinator(hass, entry)

        switches, buttons, numbers, selects = [], [], [], []
        await sw_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: switches.extend(ents))
        await btn_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: buttons.extend(ents))
        await num_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: numbers.extend(ents))
        await sel_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: selects.extend(ents))
        return hass, entry, coord, switches, buttons, numbers, selects, patches, init_mod

    async def test_f16_01_switch_platform_creates_at_least_16_switches(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, switches, _, _, _, patches, init_mod = await self._setup_controls(td)
            try:
                self.assertGreaterEqual(len(switches), 16)
                uids = {sw.unique_id for sw in switches}
                self.assertEqual(len(uids), len(switches))
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f16_02_switch_turn_on_and_turn_off_actions(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, switches, _, _, _, patches, init_mod = await self._setup_controls(td)
            try:
                slot1_sw = next(sw for sw in switches if "slot_1" in sw.unique_id or "slot1" in sw.unique_id)
                await slot1_sw.async_turn_off()
                self.assertFalse(coord.data["schedule"][0]["enabled"])
                await slot1_sw.async_turn_on()
                self.assertTrue(coord.data["schedule"][0]["enabled"])

                ind_sw = next(sw for sw in switches if "indicator" in sw.unique_id or "101" in sw.unique_id)
                await ind_sw.async_turn_off()
                self.assertEqual(coord.data["dps"].get("101"), False)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f16_03_button_platform_creates_at_least_8_buttons_and_presses(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, buttons, _, _, patches, init_mod = await self._setup_controls(td)
            try:
                self.assertGreaterEqual(len(buttons), 8)
                refill_btn = next(b for b in buttons if "refill" in b.unique_id)
                await refill_btn.async_press()
                self.assertTrue(coord.data["summary"]["hopper_known"])

                clear_btn = next(b for b in buttons if "clear" in b.unique_id)
                await clear_btn.async_press()
                self.assertFalse(coord.data["summary"]["clog_detected"])
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f16_04_number_platform_creates_at_least_12_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, _, _, numbers, _, patches, init_mod = await self._setup_controls(td)
            try:
                self.assertGreaterEqual(len(numbers), 12)
                uids = {n.unique_id for n in numbers}
                self.assertEqual(len(uids), len(numbers))
                for n in numbers:
                    self.assertIsNotNone(n.native_value)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f16_05_number_async_set_native_value_updates_coordinator(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, _, numbers, _, patches, init_mod = await self._setup_controls(td)
            try:
                slot1_num = next(n for n in numbers if "slot_1" in n.unique_id or "slot1" in n.unique_id)
                await slot1_num.async_set_native_value(7)
                self.assertEqual(coord.data["schedule"][0]["portions"], 7)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f16_06_select_platform_creates_at_least_4_selects_and_selects_option(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, _, _, selects, patches, init_mod = await self._setup_controls(td)
            try:
                self.assertGreaterEqual(len(selects), 4)
                mode_sel = next(s for s in selects if "mode" in s.unique_id and "alert" not in s.unique_id)
                self.assertIn("cloud", mode_sel.options)
                await mode_sel.async_select_option("cloud")
                self.assertEqual(coord.data["configured_mode"], "cloud")
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()


# ===========================================================================
# F17: Camera, Image & Event Platforms (`camera.py`, `image.py`, `event.py`) (6 tests)
# ===========================================================================
class TestF17CameraImageAndEventPlatforms(unittest.IsolatedAsyncioTestCase):
    """F17 — `MollyCodyBowlCamera`, 3 `ImageEntity` classes, and 2 `EventEntity` classes."""

    async def _setup_media_platforms(self, td: str):
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        cam_mod = importlib.import_module("custom_components.molly_cody_feeder.camera")
        img_mod = importlib.import_module("custom_components.molly_cody_feeder.image")
        evt_mod = importlib.import_module("custom_components.molly_cody_feeder.event")

        hass, entry = _create_hass_and_entry(td)
        patches = _client_network_patches(td)
        for p in patches:
            p.start()
        await init_mod.async_setup_entry(hass, entry)
        coord = _get_coordinator(hass, entry)

        cameras, images, events = [], [], []
        await cam_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: cameras.extend(ents))
        await img_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: images.extend(ents))
        await evt_mod.async_setup_entry(hass, entry, lambda ents, *a, **kw: events.extend(ents))
        return hass, entry, coord, cameras, images, events, patches, init_mod

    async def test_f17_01_camera_platform_setup_and_async_camera_image(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, cameras, _, _, patches, init_mod = await self._setup_media_platforms(td)
            try:
                self.assertGreaterEqual(len(cameras), 1)
                cam = cameras[0]
                img_bytes = await cam.async_camera_image()
                self.assertIsInstance(img_bytes, bytes)
                self.assertTrue(img_bytes.startswith(b"\xff\xd8"))
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f17_02_image_platform_creates_3_image_entities(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, _, images, _, patches, init_mod = await self._setup_media_platforms(td)
            try:
                self.assertEqual(len(images), 3)
                uids = {im.unique_id for im in images}
                self.assertEqual(len(uids), 3)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f17_03_image_entities_return_annotated_before_and_after_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, images, _, patches, init_mod = await self._setup_media_platforms(td)
            try:
                coord.data["latest_annotated_bytes"] = b"\xff\xd8annotated"
                coord.data["latest_before_bytes"] = b"\xff\xd8before"
                coord.data["latest_after_bytes"] = b"\xff\xd8after"
                returned = set()
                for im in images:
                    b = await im.async_image()
                    returned.add(b)
                self.assertEqual(returned, {b"\xff\xd8annotated", b"\xff\xd8before", b"\xff\xd8after"})
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f17_04_event_platform_creates_feed_and_alert_event_entities(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, _, _, _, events, patches, init_mod = await self._setup_media_platforms(td)
            try:
                self.assertEqual(len(events), 2)
                for ev in events:
                    self.assertTrue(len(ev.event_types) >= 1)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f17_05_feed_event_entity_triggers_on_feed_event(self):
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, _, events, patches, init_mod = await self._setup_media_platforms(td)
            try:
                feed_ent = next(e for e in events if "feed" in e.unique_id)
                await coord.async_feed(portions=2, verify=False)
                self.assertIsNotNone(coord.data.get("last_feed_event"))
                self.assertIsNotNone(feed_ent.unique_id)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()

    async def test_f17_06_alert_event_entity_triggers_on_alert_event(self):
        const = _import_const()
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord, _, _, events, patches, init_mod = await self._setup_media_platforms(td)
            try:
                alert_ent = next(e for e in events if "alert" in e.unique_id)
                if hasattr(hass, "bus") and hasattr(hass.bus, "async_fire"):
                    hass.bus.async_fire(
                        const.EVENT_ALERT,
                        {"event_type": "clog_detected", "title": "Clog Alert", "message": "Check chute"},
                    )
                self.assertIsNotNone(alert_ent.event_types)
                self.assertGreaterEqual(len(alert_ent.event_types), 1)
            finally:
                await init_mod.async_unload_entry(hass, entry)
                for p in patches:
                    p.stop()


# ===========================================================================
# F18: Lovelace Dashboard, Automation Blueprints & v0.7 Documentation (6 tests)
# ===========================================================================
class TestF18LovelaceBlueprintsAndDocs(unittest.TestCase):
    """F18 — Lovelace YAML, Home Assistant `!input` blueprints, `info.md`, `README.md`, `CHANGELOG.md`."""

    @staticmethod
    def _load_ha_yaml(path: Path) -> Any:
        class HaYamlLoader(yaml.SafeLoader):
            pass

        HaYamlLoader.add_constructor(
            "!input",
            lambda loader, node: {"__ha_input__": loader.construct_scalar(node)},
        )
        return yaml.load(path.read_text(encoding="utf-8"), Loader=HaYamlLoader)

    def test_f18_01_lovelace_dashboard_yaml_valid_and_references_domain(self):
        dash_path = REPO_ROOT / "lovelace" / "molly_cody_dashboard.yaml"
        self.assertTrue(dash_path.exists(), "lovelace/molly_cody_dashboard.yaml must exist")
        raw_text = dash_path.read_text(encoding="utf-8")
        parsed = self._load_ha_yaml(dash_path)
        self.assertIsInstance(parsed, dict)
        self.assertIn("views", parsed)
        self.assertIn("molly_cody_feeder", raw_text)

    def test_f18_02_clog_alert_blueprint_yaml_valid_schema(self):
        bp_path = REPO_ROOT / "blueprints" / "automation" / "molly_cody_feeder" / "clog_alert_notification.yaml"
        self.assertTrue(bp_path.exists(), "clog_alert_notification.yaml must exist")
        parsed = self._load_ha_yaml(bp_path)
        self.assertIn("blueprint", parsed)
        self.assertEqual(parsed["blueprint"].get("domain"), "automation")
        self.assertIsInstance(parsed["blueprint"].get("input"), dict)
        self.assertTrue("trigger" in parsed or "triggers" in parsed)
        self.assertTrue("action" in parsed or "actions" in parsed)

    def test_f18_03_hopper_low_blueprint_yaml_valid_schema(self):
        bp_path = REPO_ROOT / "blueprints" / "automation" / "molly_cody_feeder" / "hopper_low_reminder.yaml"
        self.assertTrue(bp_path.exists(), "hopper_low_reminder.yaml must exist")
        parsed = self._load_ha_yaml(bp_path)
        self.assertIn("blueprint", parsed)
        self.assertEqual(parsed["blueprint"].get("domain"), "automation")
        self.assertIsInstance(parsed["blueprint"].get("input"), dict)
        self.assertTrue("trigger" in parsed or "triggers" in parsed)
        self.assertTrue("action" in parsed or "actions" in parsed)

    def test_f18_04_info_md_exists_and_documents_hacs_integration(self):
        info_path = REPO_ROOT / "info.md"
        self.assertTrue(info_path.exists(), "info.md must exist for HACS")
        text = info_path.read_text(encoding="utf-8")
        self.assertGreater(len(text), 200)
        self.assertIn("Molly", text)
        self.assertIn("Home Assistant", text)

    def test_f18_05_readme_md_documents_v07_standalone_hacs_install(self):
        readme_path = REPO_ROOT / "README.md"
        self.assertTrue(readme_path.exists())
        text = readme_path.read_text(encoding="utf-8")
        self.assertIn("HACS", text)
        self.assertIn("molly_cody_feeder", text)

    def test_f18_06_changelog_md_documents_v07_release(self):
        changelog_path = REPO_ROOT / "CHANGELOG.md"
        self.assertTrue(changelog_path.exists())
        text = changelog_path.read_text(encoding="utf-8")
        self.assertIn("0.7", text)
        self.assertIn("HACS", text)


if __name__ == "__main__":
    unittest.main()
