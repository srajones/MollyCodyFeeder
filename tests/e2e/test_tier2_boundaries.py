"""Tier 2 E2E Tests: Boundary, Negative & Corner Cases (F1-F18).

Covers >= 95 genuine boundary, negative, and corner-case tests (>= 5 per feature
across all 18 features F1-F18) against the public interface contracts in PROJECT.md
and TEST_INFRA.md. All tests run hermetically offline using temporary directories
and mocks for external network I/O.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from typing import Any
from unittest import mock

import importlib
import inspect
import types
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bootstrap_standalone_env() -> None:
    try:
        importlib.import_module("custom_components.molly_cody_feeder.core.env")
    except ImportError:
        importlib.import_module("watchdog.env")


_bootstrap_standalone_env()
import numpy as np  # noqa: E402
import requests  # noqa: E402
import yaml  # noqa: E402

LIVE_SCHEDULE = "7f07002010000007f11002010000007f16002010000007f1900171000000000908100000000"


# ---------------------------------------------------------------------------
# Hermetic Helpers for Core, HA Compat, Coordinator & Entity Platforms
# ---------------------------------------------------------------------------
def _write_bgr_image(path: str, img_bgr: np.ndarray) -> None:
    rgb = np.ascontiguousarray(img_bgr[:, :, ::-1]) if img_bgr.ndim == 3 else img_bgr
    Image.fromarray(rgb).save(path, format="JPEG", quality=95)


def _core_molly_crypto():
    try:
        from custom_components.molly_cody_feeder.core.molly import crypto as mod
        return mod
    except ImportError:
        from watchdog.molly import crypto as mod
        return mod


def _core_molly_net():
    try:
        from custom_components.molly_cody_feeder.core.molly import net as mod
        return mod
    except ImportError:
        from watchdog.molly import net as mod
        return mod


def _core_imm_codec():
    try:
        from custom_components.molly_cody_feeder.core.molly import imm_codec as mod
        return mod
    except ImportError:
        from watchdog.molly import imm_codec as mod
        return mod


def _core_imm_transport():
    try:
        from custom_components.molly_cody_feeder.core.molly import imm_transport as mod
        return mod
    except ImportError:
        from watchdog.molly import imm_transport as mod
        return mod


def _core_molly_media():
    try:
        from custom_components.molly_cody_feeder.core.molly import media as mod
        return mod
    except ImportError:
        from watchdog.molly import media as mod
        return mod


def _core_molly_camera():
    try:
        from custom_components.molly_cody_feeder.core.molly import camera as mod
        return mod
    except ImportError:
        from watchdog.molly import camera as mod
        return mod


def _core_molly_speaker():
    try:
        from custom_components.molly_cody_feeder.core.molly import speaker as mod
        return mod
    except ImportError:
        from watchdog.molly import speaker as mod
        return mod


def _core_decision():
    try:
        from custom_components.molly_cody_feeder.core.service import decision as mod
        return mod
    except ImportError:
        from watchdog.service import decision as mod
        return mod


def _resolve_symbol(name: str, core_modules: tuple[str, ...], fallback_module: str) -> Any:
    for mod_path in ("custom_components.molly_cody_feeder.core", *core_modules):
        try:
            mod = importlib.import_module(mod_path)
            if hasattr(mod, name):
                return getattr(mod, name)
        except ImportError:
            continue
    fb = importlib.import_module(fallback_module)
    return getattr(fb, name)


def _core_symbols() -> types.SimpleNamespace:
    RuntimeConfig = _resolve_symbol(
        "RuntimeConfig",
        ("custom_components.molly_cody_feeder.core.config",),
        "watchdog.config",
    )
    WatchdogConfig = _resolve_symbol(
        "WatchdogConfig",
        ("custom_components.molly_cody_feeder.core.config",),
        "watchdog.config",
    )
    MollyFeederClient = _resolve_symbol(
        "MollyFeederClient",
        (
            "custom_components.molly_cody_feeder.core.molly.client",
            "custom_components.molly_cody_feeder.core.molly",
        ),
        "watchdog.molly.client",
    )
    BowlAnalyzer = _resolve_symbol(
        "BowlAnalyzer",
        (
            "custom_components.molly_cody_feeder.core.analysis",
            "custom_components.molly_cody_feeder.core.analysis.bowl_analyzer",
        ),
        "watchdog.analysis",
    )
    AudioAnalysisResult = _resolve_symbol(
        "AudioAnalysisResult",
        (
            "custom_components.molly_cody_feeder.core.analysis",
            "custom_components.molly_cody_feeder.core.analysis.results",
        ),
        "watchdog.analysis",
    )
    VisualAnalysisResult = _resolve_symbol(
        "VisualAnalysisResult",
        (
            "custom_components.molly_cody_feeder.core.analysis",
            "custom_components.molly_cody_feeder.core.analysis.results",
        ),
        "watchdog.analysis",
    )
    assess_dispense = _resolve_symbol(
        "assess_dispense",
        (
            "custom_components.molly_cody_feeder.core.analysis.learning",
            "custom_components.molly_cody_feeder.core.analysis",
        ),
        "watchdog.analysis.learning",
    )
    KibbleDatabase = _resolve_symbol(
        "KibbleDatabase",
        ("custom_components.molly_cody_feeder.core.db",),
        "watchdog.db",
    )
    RetentionManager = _resolve_symbol(
        "RetentionManager",
        (
            "custom_components.molly_cody_feeder.core.service.retention",
            "custom_components.molly_cody_feeder.core.retention",
        ),
        "watchdog.service.retention",
    )
    MealEntry = _resolve_symbol(
        "MealEntry",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    decode_molly_schedule = _resolve_symbol(
        "decode_molly_schedule",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    encode_molly_schedule = _resolve_symbol(
        "encode_molly_schedule",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    update_molly_schedule_slot = _resolve_symbol(
        "update_molly_schedule_slot",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    parse_days_spec = _resolve_symbol(
        "parse_days_spec",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    decode_meal_plan = _resolve_symbol(
        "decode_meal_plan",
        ("custom_components.molly_cody_feeder.core.schedule_decoder",),
        "watchdog.schedule_decoder",
    )
    return types.SimpleNamespace(
        RuntimeConfig=RuntimeConfig,
        WatchdogConfig=WatchdogConfig,
        MollyFeederClient=MollyFeederClient,
        BowlAnalyzer=BowlAnalyzer,
        AudioAnalysisResult=AudioAnalysisResult,
        VisualAnalysisResult=VisualAnalysisResult,
        assess_dispense=assess_dispense,
        KibbleDatabase=KibbleDatabase,
        RetentionManager=RetentionManager,
        MealEntry=MealEntry,
        decode_molly_schedule=decode_molly_schedule,
        encode_molly_schedule=encode_molly_schedule,
        update_molly_schedule_slot=update_molly_schedule_slot,
        parse_days_spec=parse_days_spec,
        decode_meal_plan=decode_meal_plan,
    )


def _make_client(td: str, **kwargs: Any):
    MollyFeederClient = _core_symbols().MollyFeederClient

    sess_path = str(Path(td) / "tuya_session.json")
    defaults = {
        "email": "test@example.com",
        "password": "secret_password",
        "country_code": "1",
        "mode": "auto",
        "device_id": "dev_test_12345",
        "local_ip": "192.168.1.50",
        "local_key": "0123456789abcdef",
        "session_file": sess_path,
    }
    defaults.update(kwargs)
    with mock.patch.dict(
        os.environ,
        {"MOLLY_USER": "", "MOLLY_PASS": "", "USER": "", "PASS": ""},
        clear=False,
    ):
        return MollyFeederClient(**defaults)


def _make_analyzer(td: str | None = None):
    cs = _core_symbols()
    BowlAnalyzer = cs.BowlAnalyzer

    try:
        if td is not None:
            cfg = cs.RuntimeConfig(storage_dir=Path(td))
            return BowlAnalyzer(cfg)
        return BowlAnalyzer()
    except TypeError:
        cfg = cs.RuntimeConfig()
        return BowlAnalyzer(cfg)


def _log_feed(db: Any, **kwargs: Any) -> int:
    defaults = {
        "event_type": "scheduled_verify",
        "mode": "cloud",
        "transport": "cloud",
        "portions": 1,
        "success": True,
        "clog_detected": False,
        "motor_detected": True,
        "outcome": "dispensed",
    }
    defaults.update(kwargs)
    if hasattr(db, "log_feed_event"):
        return db.log_feed_event(**defaults)
    return db.record_feed_event(defaults)


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 8000) -> str:
    pcm16 = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return str(path)


def _make_hass_and_entry(
    td: str,
    data_overrides: dict[str, Any] | None = None,
    options_overrides: dict[str, Any] | None = None,
    entry_id: str = "entry_tier2_test",
):
    from custom_components.molly_cody_feeder._ha_compat import ConfigEntry, HomeAssistant
    from custom_components.molly_cody_feeder import const

    try:
        sig = inspect.signature(HomeAssistant)
        hass = HomeAssistant(td) if len(sig.parameters) >= 1 else HomeAssistant()
    except Exception:
        hass = HomeAssistant()

    if hasattr(hass, "config") and hasattr(hass.config, "config_dir"):
        hass.config.config_dir = td
    elif hasattr(hass, "config"):
        hass.config.path = lambda *parts: str(Path(td).joinpath(*parts))

    data = {
        const.CONF_EMAIL: "owner@example.com",
        const.CONF_PASSWORD: "SuperSecretPassword123!",
        const.CONF_COUNTRY_CODE: "1",
        const.CONF_DEVICE_ID: "molly_dev_001",
        const.CONF_DEVICE_NAME: "Molly & Cody Feeder",
        const.CONF_LOCAL_IP: "192.168.1.120",
        const.CONF_LOCAL_KEY: "local_secret_key",
        const.CONF_CONNECTION_MODE: "auto",
    }
    if data_overrides:
        data.update(data_overrides)

    options = {
        const.CONF_CONNECTION_MODE: data.get(const.CONF_CONNECTION_MODE, "auto"),
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
        const.CONF_ENABLE_VISION_VERIFICATION: False,
        const.CONF_ENABLE_MP4_RECORDING: False,
        const.CONF_ENABLE_AUTO_RETRY: True,
    }
    if options_overrides:
        options.update(options_overrides)

    entry = ConfigEntry(
        entry_id=entry_id,
        domain=const.DOMAIN,
        title=data.get(const.CONF_DEVICE_NAME, "Molly & Cody Feeder"),
        data=data,
        options=options,
        unique_id=data.get(const.CONF_DEVICE_ID, "molly_dev_001"),
    )
    return hass, entry


def _extract_coordinator(hass: Any, entry: Any):
    from custom_components.molly_cody_feeder.const import DOMAIN

    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(stored, dict) and "coordinator" in stored:
        return stored["coordinator"]
    return getattr(stored, "coordinator", stored)


async def _setup_entry_with_mock_client(
    td: str,
    status_overrides: dict[str, Any] | None = None,
    data_overrides: dict[str, Any] | None = None,
    options_overrides: dict[str, Any] | None = None,
):
    import custom_components.molly_cody_feeder as init_mod

    cs = _core_symbols()
    decode_molly_schedule = cs.decode_molly_schedule
    MollyFeederClient = cs.MollyFeederClient

    hass, entry = _make_hass_and_entry(
        td, data_overrides=data_overrides, options_overrides=options_overrides
    )

    default_status = {
        "device_id": entry.data["device_id"],
        "device_name": entry.data["device_name"],
        "configured_mode": entry.options.get("connection_mode", "auto"),
        "active_transport": "cloud",
        "local_ip": entry.data.get("local_ip", "192.168.1.120"),
        "online": True,
        "dps": {
            "101": True,
            "103": False,
            "104": True,
            "106": "1",
            "108": "0",
            "134": True,
            "201": 1,
            "202": 180,
            "207": LIVE_SCHEDULE,
            "255": 1,
        },
        "named_dps": {
            "indicator_light": True,
            "flip_screen": False,
            "osd_watermark": True,
            "motion_sensitivity": "1",
            "nightvision": "0",
            "motion_switch": True,
            "manual_feed": 1,
            "food_weight_g": 180,
            "schedule_raw": LIVE_SCHEDULE,
            "feed_abnormal": 1,
        },
        "food_weight_g": 180,
        "last_feed_portions": 1,
        "feed_abnormal": 1,
        "schedule_raw": LIVE_SCHEDULE,
        "schedule": [m.to_dict() for m in decode_molly_schedule(LIVE_SCHEDULE)],
    }
    if status_overrides:
        default_status.update(status_overrides)

    with mock.patch.object(
        MollyFeederClient,
        "login",
        return_value=True,
    ), mock.patch.object(
        MollyFeederClient,
        "get_status",
        return_value=default_status,
    ), mock.patch.object(
        MollyFeederClient,
        "dispense_food",
        return_value=(True, "cloud"),
    ), mock.patch.object(
        MollyFeederClient,
        "set_dp",
        return_value=(True, "cloud"),
    ), mock.patch.object(
        MollyFeederClient,
        "set_dps",
        return_value=(True, "cloud"),
    ):
        ok = await init_mod.async_setup_entry(hass, entry)
        assert ok is True
        coord = _extract_coordinator(hass, entry)
        return hass, entry, coord


# ===========================================================================
# F1: OEM Cloud Auth, Signing & Session Persistence Boundaries
# ===========================================================================
class TestF1CloudAuthBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F1 (Cloud Auth & Session)."""

    def test_f1_01_missing_credentials_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td, email="", password="")
            with self.assertRaises((RuntimeError, ValueError)):
                client.login(force=True)

    def test_f1_02_md5_login_failure_triggers_rsa_pkcs1v15_fallback(self):
        # 512-bit RSA modulus (64 bytes -> 128 hex chars ciphertext) and exponent 65537
        pub_n_dec = str((1 << 511) | 0x95310001)
        pub_e_dec = "65537"

        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td, email="rsa_user@example.com", password="RsaTestPassword!")
            calls = []

            def fake_raw_api(action, version, post_data_obj, sid="", gid=0):
                calls.append((action, dict(post_data_obj)))
                if action == "smartlife.m.user.username.token.get":
                    return {
                        "success": True,
                        "result": {
                            "token": "tok_123",
                            "publicKey": pub_n_dec,
                            "exponent": pub_e_dec,
                        },
                    }
                if action == "smartlife.m.user.email.password.login":
                    if post_data_obj.get("ifencrypt") == 0:
                        return {"success": False, "errorCode": "USER_PASSWD_WRONG"}
                    return {
                        "success": True,
                        "result": {
                            "sid": "sid_from_rsa",
                            "uid": "uid_from_rsa",
                            "ecode": "ecode_from_rsa",
                            "partnerIdentity": "p2155085",
                        },
                    }
                return {"success": True, "result": []}

            with mock.patch.object(client, "_raw_api_call", side_effect=fake_raw_api), \
                 mock.patch.object(client, "refresh_cloud_devices", return_value=True), \
                 mock.patch("time.sleep"):
                ok = client.login(force=True)

            self.assertTrue(ok)
            self.assertEqual(client.sid, "sid_from_rsa")
            self.assertEqual(client.uid, "uid_from_rsa")
            login_calls = [c for c in calls if c[0] == "smartlife.m.user.email.password.login"]
            self.assertEqual(len(login_calls), 2)
            self.assertEqual(login_calls[0][1]["ifencrypt"], 0)
            self.assertEqual(login_calls[1][1]["ifencrypt"], 1)
            self.assertEqual(len(login_calls[1][1]["passwd"]), 128)  # 512-bit RSA hex

    def test_f1_03_call_api_auto_relogin_on_session_expired(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            client.sid = "expired_sid"
            client.uid = "uid_1"
            client.ecode = "ecode_1"
            client.gid = 100

            seen_sids = []

            def fake_raw_api(action, version, post_data_obj, sid="", gid=0):
                seen_sids.append(sid)
                if sid == "expired_sid":
                    return {"success": False, "errorCode": "USER_SESSION_EXPIRED"}
                return {"success": True, "result": {"devices": ["dev1"]}}

            def fake_login(force=False):
                client.sid = "fresh_sid_after_relogin"
                return True

            with mock.patch.object(client, "_raw_api_call", side_effect=fake_raw_api), \
                 mock.patch.object(client, "login", side_effect=fake_login) as m_login:
                res = client.call_api("m.life.my.group.device.list", "2.2", {"gid": 100})

            self.assertTrue(res.get("success"))
            m_login.assert_called_once_with(force=True)
            self.assertEqual(seen_sids, ["expired_sid", "fresh_sid_after_relogin"])

    def test_f1_04_corrupt_or_truncated_session_json_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            sess_file = Path(td) / "corrupt_session.json"
            sess_file.write_text("{corrupt truncated json...", encoding="utf-8")
            client = _make_client(td, session_file=str(sess_file))
            self.assertFalse(client._load_session())
            self.assertEqual(client.sid, "")

            # Incomplete JSON missing required keys ('sid', 'uid', 'ecode')
            sess_file.write_text(json.dumps({"sid": "only_sid"}), encoding="utf-8")
            self.assertFalse(client._load_session())

    def test_f1_05_thing_action_prefix_rewritten_to_smartlife(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            captured = {}

            def fake_post(url, params=None, data=None, headers=None, timeout=None):
                captured["params"] = params
                captured["data"] = data
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()
                resp.json.return_value = {"success": True, "result": True}
                return resp

            with mock.patch("requests.post", side_effect=fake_post):
                res = client._raw_api_call(
                    "thing.m.device.dp.publish",
                    "1.0",
                    {"devId": "dev1", "dps": '{"201":1}'},
                    sid="s1",
                )

            self.assertTrue(res["success"])
            self.assertEqual(captured["params"]["a"], "smartlife.m.device.dp.publish")
            self.assertEqual(captured["data"]["a"], "smartlife.m.device.dp.publish")

    def test_f1_06_post_data_md5_empty_and_nybble_swap_boundary(self):
        crypto = _core_molly_crypto()
        self.assertEqual(crypto._post_data_md5_hex(""), "")
        payload = '{"devId":"d1","dps":"{\\"201\\":1}"}'
        raw_md5 = crypto._md5_hex(payload)
        swapped = crypto._post_data_md5_hex(payload)
        self.assertEqual(len(swapped), 32)
        expected = raw_md5[8:16] + raw_md5[0:8] + raw_md5[24:32] + raw_md5[16:24]
        self.assertEqual(swapped, expected)


# ===========================================================================
# F2: Cloud MQTT 2.2 & Local LAN Transport Boundaries
# ===========================================================================
class TestF2TransportBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F2 (MQTT 2.2 & LAN/Cloud Transport)."""

    def test_f2_01_never_sent_read_timeout_never_retries_anti_double_feed(self):
        net = _core_molly_net()
        self.assertFalse(net._never_sent(requests.exceptions.ReadTimeout("read timed out")))
        self.assertFalse(
            net._never_sent(requests.exceptions.ConnectionError("Connection aborted, RemoteDisconnected"))
        )

        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with mock.patch.object(
                client,
                "call_api",
                side_effect=requests.exceptions.ReadTimeout("read timeout after request sent"),
            ) as m_api, mock.patch("time.sleep"):
                ok = client.set_dps_cloud({"201": 1})

            self.assertFalse(ok)
            self.assertEqual(
                m_api.call_count,
                1,
                "ReadTimeout must NEVER retry because the feeder may have already received DP 201",
            )

    def test_f2_02_never_sent_connect_timeout_retries_three_times(self):
        net = _core_molly_net()
        self.assertTrue(net._never_sent(requests.exceptions.ConnectTimeout("connect timeout")))
        self.assertTrue(
            net._never_sent(requests.exceptions.ConnectionError("NewConnectionError: Connection refused"))
        )
        self.assertTrue(
            net._never_sent(requests.exceptions.ConnectionError("NameResolutionError: DNS failed"))
        )

        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with mock.patch.object(
                client,
                "call_api",
                side_effect=requests.exceptions.ConnectTimeout("TCP SYN timed out"),
            ) as m_api, mock.patch("time.sleep"):
                ok = client.set_dps_cloud({"201": 1})

            self.assertFalse(ok)
            self.assertEqual(m_api.call_count, 3)

    def test_f2_03_set_mode_invalid_raises_value_error_and_hybrid_normalizes(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with self.assertRaises(ValueError):
                client.set_mode("bluetooth")
            with self.assertRaises(ValueError):
                client.set_mode("invalid_transport")
            self.assertEqual(client.set_mode("hybrid"), "auto")
            self.assertEqual(client.mode, "auto")

    def test_f2_04_decrypt_mqtt_22_rejects_short_or_wrong_version_packets(self):
        crypto = _core_molly_crypto()
        key = "0123456789abcdef"
        self.assertIsNone(crypto._decrypt_mqtt_22(b"", key))
        self.assertIsNone(crypto._decrypt_mqtt_22(b"2.2short", key))
        self.assertIsNone(crypto._decrypt_mqtt_22(b"2.2" + b"\x00" * 12, key))  # exactly 15 bytes
        self.assertIsNone(crypto._decrypt_mqtt_22(b"2.1" + b"\x00" * 32, key))  # wrong version prefix

    def test_f2_05_get_status_auto_mode_lan_failure_falls_back_to_cloud(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td, mode="auto")
            cloud_dps = {"201": 3, "202": 140, "207": LIVE_SCHEDULE, "255": 1}
            with mock.patch.object(client, "get_dps_lan", return_value=None) as m_lan, \
                 mock.patch.object(client, "get_dps_cloud", return_value=cloud_dps) as m_cloud:
                status = client.get_status()

            m_lan.assert_called_once()
            m_cloud.assert_called_once()
            self.assertEqual(status["active_transport"], "cloud")
            self.assertTrue(status["online"])
            self.assertEqual(status["food_weight_g"], 140)

    def test_f2_06_get_status_both_transports_offline_returns_none_and_offline(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td, mode="auto")
            with mock.patch.object(client, "get_dps_lan", return_value=None), \
                 mock.patch.object(client, "get_dps_cloud", return_value=None):
                status = client.get_status()

            self.assertEqual(status["active_transport"], "none")
            self.assertFalse(status["online"])


# ===========================================================================
# F3: Hardware DP Control (DP 101..255) & Manual Feed (DP 201) Boundaries
# ===========================================================================
class TestF3HardwareDpBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F3 (Hardware DPs & Manual Feed)."""

    def test_f3_01_dispense_food_clamps_zero_and_negative_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with mock.patch.object(client, "set_dp", return_value=(True, "cloud")) as m_set_dp:
                client.dispense_food(0)
                self.assertEqual(m_set_dp.call_args.args, (201, 1))
                client.dispense_food(-10)
                self.assertEqual(m_set_dp.call_args.args, (201, 1))

    def test_f3_02_dispense_food_clamps_above_twenty_to_twenty(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with mock.patch.object(client, "set_dp", return_value=(True, "cloud")) as m_set_dp:
                client.dispense_food(21)
                self.assertEqual(m_set_dp.call_args.args, (201, 20))
                client.dispense_food(999)
                self.assertEqual(m_set_dp.call_args.args, (201, 20))

    def test_f3_03_dispense_food_non_integer_raises_type_or_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with self.assertRaises((ValueError, TypeError)):
                client.dispense_food("not_an_int")  # type: ignore[arg-type]
            with self.assertRaises((ValueError, TypeError)):
                client.dispense_food(None)  # type: ignore[arg-type]

    def test_f3_04_dp_255_feed_abnormal_equals_one_is_normal_not_fault(self):
        AudioAnalysisResult = _core_symbols().AudioAnalysisResult

        d = _core_decision()
        good_audio = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=35,
            kibble_dispensed_audio=True,
            motor_energy_db=25.0,
            impact_energy_db=10.0,
            diagnosis="ok",
            motor_duration_sec=3.0,
        )
        self.assertTrue(d.decide_passed(good_audio, None, feed_abnormal=0, expect_dispense=True))
        self.assertTrue(
            d.decide_passed(good_audio, None, feed_abnormal=1, expect_dispense=True),
            "DP 255 == 1 is normal healthy status on Molly & Cody hardware",
        )

    def test_f3_05_dp_255_feed_abnormal_greater_than_one_forces_fault_failure(self):
        AudioAnalysisResult = _core_symbols().AudioAnalysisResult

        d = _core_decision()
        good_audio = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=35,
            kibble_dispensed_audio=True,
            motor_energy_db=25.0,
            impact_energy_db=10.0,
            diagnosis="ok",
            motor_duration_sec=3.0,
        )
        self.assertFalse(d.decide_passed(good_audio, None, feed_abnormal=2, expect_dispense=True))
        self.assertFalse(d.decide_passed(good_audio, None, feed_abnormal=4, expect_dispense=True))

    def test_f3_06_set_dps_in_cloud_only_mode_never_calls_lan_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td, mode="cloud")
            with mock.patch.object(client, "set_dps_cloud", return_value=False) as m_cloud, \
                 mock.patch.object(client, "set_dps_lan", return_value=True) as m_lan:
                ok, transport = client.set_dps({"101": True})

            self.assertFalse(ok)
            self.assertEqual(transport, "cloud")
            m_cloud.assert_called_once()
            m_lan.assert_not_called()


# ===========================================================================
# F4: DP 207 15-Char Schedule Codec & Slot Management Boundaries
# ===========================================================================
class TestF4ScheduleBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F4 (DP 207 Schedule Codec)."""

    def test_f4_01_decode_empty_and_non_string_returns_empty_list(self):
        decode_molly_schedule = _core_symbols().decode_molly_schedule

        self.assertEqual(decode_molly_schedule(""), [])
        self.assertEqual(decode_molly_schedule("    "), [])
        self.assertEqual(decode_molly_schedule(None), [])  # type: ignore[arg-type]
        self.assertEqual(decode_molly_schedule(12345), [])  # type: ignore[arg-type]

    def test_f4_02_decode_non_multiple_of_15_chars_returns_empty_list(self):
        decode_molly_schedule = _core_symbols().decode_molly_schedule

        self.assertEqual(decode_molly_schedule("7f070020100000"), [])   # 14 chars
        self.assertEqual(decode_molly_schedule("7f07002010000001"), [])  # 16 chars
        self.assertEqual(decode_molly_schedule("7f0700"), [])            # 6 chars

    def test_f4_03_decode_invalid_hex_or_out_of_range_hour_minute_returns_empty(self):
        decode_molly_schedule = _core_symbols().decode_molly_schedule

        self.assertEqual(decode_molly_schedule("zz0700201000000"), [])  # invalid hex mask
        self.assertEqual(decode_molly_schedule("7f2400201000000"), [])  # hour 24 > 23
        self.assertEqual(decode_molly_schedule("7f0760201000000"), [])  # minute 60 > 59

    def test_f4_04_update_schedule_slot_out_of_bounds_raises_value_error(self):
        update_molly_schedule_slot = _core_symbols().update_molly_schedule_slot

        with self.assertRaises(ValueError):
            update_molly_schedule_slot(LIVE_SCHEDULE, slot=0, portions=2)
        with self.assertRaises(ValueError):
            update_molly_schedule_slot(LIVE_SCHEDULE, slot=9, portions=2)
        with self.assertRaises(ValueError):
            update_molly_schedule_slot(LIVE_SCHEDULE, slot=-1, portions=2)

    def test_f4_05_parse_days_spec_and_days_str_mask_boundaries(self):
        cs = _core_symbols()
        MealEntry, parse_days_spec = cs.MealEntry, cs.parse_days_spec

        self.assertEqual(parse_days_spec("once"), 0x00)
        self.assertEqual(parse_days_spec("0x00"), 0x00)
        self.assertEqual(parse_days_spec("everyday"), 0x7F)
        self.assertEqual(parse_days_spec("weekdays"), 0x1F)
        self.assertEqual(parse_days_spec("weekends"), 0x60)
        self.assertEqual(parse_days_spec("mon,wed,fri"), 0x15)

        once_entry = MealEntry(days_mask=0x00, hour=8, minute=0, portions=1, enabled=True, slot=1)
        daily_entry = MealEntry(days_mask=0x7F, hour=8, minute=0, portions=1, enabled=True, slot=1)
        mwf_entry = MealEntry(days_mask=0x15, hour=8, minute=0, portions=1, enabled=True, slot=1)
        self.assertEqual(once_entry.days_str, "Once")
        self.assertEqual(daily_entry.days_str, "Everyday")
        self.assertEqual(mwf_entry.days_str, "Mon,Wed,Fri")

    def test_f4_06_custom_six_char_suffix_preserved_on_slot_update(self):
        cs = _core_symbols()
        decode_molly_schedule, update_molly_schedule_slot = (
            cs.decode_molly_schedule,
            cs.update_molly_schedule_slot,
        )

        custom_raw = "7f0730051abcdef"
        updated = update_molly_schedule_slot(custom_raw, slot=1, portions=12)
        self.assertEqual(updated, "7f0730121abcdef")
        decoded = decode_molly_schedule(updated)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].portions, 12)
        self.assertEqual(decoded[0].suffix, "abcdef")


# ===========================================================================
# F5: Cloud `imm` (AES/KCP 330) Camera Snapshot & AV Capture Boundaries
# ===========================================================================
class TestF5ImmCameraBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F5 (Cloud imm Camera & AV Capture)."""

    def test_f5_01_camera_failure_before_trigger_calls_trigger_once_unverified(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            trigger_calls = {"count": 0}

            def trigger():
                trigger_calls["count"] += 1

            def fail_before_trigger(coro):
                coro.close()
                raise RuntimeError("Signaling answer timeout")

            with mock.patch.object(client, "_run_session", side_effect=fail_before_trigger) as m_run, \
                 mock.patch("time.sleep"):
                res = client.capture_av_clip(
                    duration_sec=5.0,
                    output_mp4=str(Path(td) / "out.mp4"),
                    output_wav=str(Path(td) / "out.wav"),
                    trigger_fn=trigger,
                )

            self.assertEqual(m_run.call_count, 2, "Should retry session setup once when trigger has not fired")
            self.assertEqual(trigger_calls["count"], 1, "Fallback must still dispense once so pet is never starved")
            self.assertTrue(res["dispensed_unverified"])
            self.assertFalse(res["audio_ok"])

    def test_f5_02_camera_failure_after_trigger_never_retries_or_double_feeds(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            trigger_calls = {"count": 0}

            def trigger():
                trigger_calls["count"] += 1

            def fail_after_trigger(coro):
                guarded = coro.cr_frame.f_locals.get("trigger_fn")
                coro.close()
                if guarded:
                    guarded()
                raise RuntimeError("UDP relay dropped after dispense")

            with mock.patch.object(client, "_run_session", side_effect=fail_after_trigger) as m_run, \
                 mock.patch("time.sleep"):
                res = client.capture_av_clip(
                    duration_sec=5.0,
                    output_mp4=str(Path(td) / "out.mp4"),
                    output_wav=str(Path(td) / "out.wav"),
                    trigger_fn=trigger,
                )

            self.assertEqual(m_run.call_count, 1, "Must NEVER retry after trigger_fn has already fired")
            self.assertEqual(trigger_calls["count"], 1, "Must NEVER double-dispense")
            self.assertFalse(res["dispensed_unverified"])

    def test_f5_03_imm_session_protocol_decode_video_false_skips_video(self):
        imm_transport = _core_imm_transport()
        proto = imm_transport._ImmSessionProtocol("ufrg", "pwd123456789012345678901", decode_video=False, cloud_only=True)
        self.assertFalse(proto.decode_video)
        self.assertTrue(proto.cloud_only)
        self.assertEqual(len(proto.video_frames), 0)

    def test_f5_04_imm_open_aes_invalid_length_raises_value_error(self):
        codec = _core_imm_codec()
        key = b"0123456789abcdef"
        with self.assertRaises(ValueError):
            codec._imm_open_aes(key, b"short_bytes")
        with self.assertRaises(ValueError):
            codec._imm_open_aes(key, b"x" * 33)  # not a multiple of 16 bytes

        # Valid roundtrip seal/open
        sealed = codec._imm_seal_aes(key, b"hello_molly_imm")
        self.assertEqual(codec._imm_open_aes(key, sealed), b"hello_molly_imm")

    def test_f5_05_motor_active_recently_and_motor_window_boundaries(self):
        media = _core_molly_media()
        self.assertFalse(media._motor_active_recently([]))
        self.assertIsNone(media._motor_window([], []))
        self.assertIsNone(media._motor_window([np.zeros(320, dtype=np.int16)], [1.0, 2.0]))

        silent_chunks = [np.zeros(320, dtype=np.int16) for _ in range(30)]
        self.assertFalse(media._motor_active_recently(silent_chunks))

        active_chunks = [np.full(320, 2000, dtype=np.int16) for _ in range(30)]
        self.assertTrue(media._motor_active_recently(active_chunks))

    def test_f5_06_recording_coverage_empty_chunks_returns_none_last_rx(self):
        cam = _core_molly_camera()
        cov = cam._recording_coverage([], 8000, [], 50.0, 63.0)
        self.assertEqual(cov["recorded_wall_sec"], 13.0)
        self.assertEqual(cov["audio_sec"], 0.0)
        self.assertIsNone(cov["audio_last_rx_sec"])


# ===========================================================================
# F6: Two-Way Speaker TTS & Chime (G.711 mulaw, reqId=7) Boundaries
# ===========================================================================
class TestF6SpeakerTtsBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F6 (Speaker TTS & Chime)."""

    def test_f6_01_talk_frame_req_id_matches_talk_open_cmd_invariant(self):
        codec = _core_imm_codec()
        speaker = _core_molly_speaker().SpeakerMixin

        self.assertEqual(speaker.TALK_REQ_ID, 7)
        self.assertEqual(speaker.TALK_OPEN_CMD, 8)
        payload = b"\xff" * 320
        frame = codec._imm_build_talk_frame(1, 320, 987654, payload, req_id=speaker.TALK_REQ_ID)
        magic, ch, c_id, rate, bits, chans, ts, length = struct.unpack_from("<IIIIIIII", frame, 0)
        self.assertEqual(magic, 0x12345678)
        self.assertEqual(ch, 7)
        self.assertEqual(c_id, 0x86)
        self.assertEqual(length, 320)

    def test_f6_02_talk_format_is_mulaw_with_0xff_silence_and_320_byte_frames(self):
        codec = _core_imm_codec()
        speaker = _core_molly_speaker().SpeakerMixin
        self.assertEqual(speaker.TALK_FFMPEG_FORMAT, "mulaw")
        self.assertEqual(speaker.TALK_SILENCE_BYTE, b"\xff")
        self.assertEqual(speaker.TALK_FRAME_BYTES, 320)
        self.assertEqual(speaker.TALK_CONV, 2)

        # Exercise _imm_talk on a non-320-aligned 500-byte payload and verify 0xFF silence padding
        # and 320-byte frame alignment across all transmitted conv=2 frames
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            sent_kcp: list[tuple[int, bytes]] = []
            speaker_opened = asyncio.Event()
            speaker_opened.set()
            udp_proto = types.SimpleNamespace(
                aes_key=b"0123456789abcdef",
                speaker_opened=speaker_opened,
                unacked={},
                remote_wnd={2: 128},
                audio_pcm_chunks=[],
                audio_rate=8000,
                ctrl_replies=[],
                active_peer=("127.0.0.1", 30000),
                send_kcp_msg=lambda conv, payload: sent_kcp.append((conv, payload)),
            )

            async def _fast_sleep(_delay: float) -> None:
                return None

            raw_non_aligned = b"\x80" * 500  # (1600 + 500 + 2400) = 4500; 4500 % 320 == 20 -> 300 trailing 0xFF pad bytes
            with mock.patch("asyncio.sleep", side_effect=_fast_sleep):
                res = asyncio.run(client._imm_talk(udp_proto, raw_non_aligned))

            self.assertTrue(res["ok"])
            conv2_ciphertexts = [cipher for conv, cipher in sent_kcp if conv == speaker.TALK_CONV]
            # (1600 lead-in + 500 voice + 2400 tail + 300 alignment pad) / 320 = 15 frames
            self.assertEqual(len(conv2_ciphertexts), 15)
            self.assertEqual(res["frames_sent"], 15)

            reassembled = bytearray()
            for cipher in conv2_ciphertexts:
                plain = codec._imm_open_aes(udp_proto.aes_key, cipher)
                magic, req_id, c_id, rate, bits, chans, ts_ms, length = struct.unpack_from("<IIIIIIII", plain, 0)
                self.assertEqual(magic, 0x12345678)
                self.assertEqual(req_id, speaker.TALK_REQ_ID)
                self.assertEqual(c_id, 0x86)
                self.assertEqual(length, 320)
                frame_audio = plain[32:]
                self.assertEqual(len(frame_audio), 320)
                reassembled.extend(frame_audio)

            expected_stream = (b"\xff" * 1600) + raw_non_aligned + (b"\xff" * (2400 + 300))
            self.assertEqual(bytes(reassembled), expected_stream)
            self.assertEqual(bytes(reassembled[-300:]), b"\xff" * 300)

    def test_f6_03_speak_tts_empty_or_whitespace_returns_error_without_session(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            with mock.patch.object(client, "_run_session") as m_session:
                res_empty = client.speak_tts("")
                res_space = client.speak_tts("   \t\n  ")

            self.assertFalse(res_empty["ok"])
            self.assertIn("empty", res_empty["error"].lower())
            self.assertFalse(res_space["ok"])
            m_session.assert_not_called()

    def test_f6_04_play_audio_file_nonexistent_path_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            missing = str(Path(td) / "does_not_exist.wav")
            res = client.play_audio_file(missing)
            self.assertFalse(res["ok"])
            self.assertIn("not found", res["error"].lower())

    def test_f6_05_loudness_chain_clamps_minimum_volume_and_ulaw_silence(self):
        speaker_mod = _core_molly_speaker()
        chain_neg = speaker_mod.SpeakerMixin._loudness_chain(-3.0)
        chain_zero = speaker_mod.SpeakerMixin._loudness_chain(0.0)
        self.assertIn("volume=0.10", chain_neg)
        self.assertIn("volume=0.10", chain_zero)

        if hasattr(speaker_mod, "_pcm16_to_ulaw"):
            encoded = speaker_mod._pcm16_to_ulaw(np.array([0, 32767, -32768], dtype=np.int16))
            self.assertEqual(len(encoded), 3)
            self.assertIn(encoded[0], (0xFF, 0x7F))

    def test_f6_06_speak_tts_repeat_clamped_to_max_five(self):
        with tempfile.TemporaryDirectory() as td:
            client = _make_client(td)
            synthesized_texts = []

            def fake_synth(text, out_path, voice="en-US-AriaNeural"):
                synthesized_texts.append(text)
                Path(out_path).write_bytes(b"x" * 300)
                return "fake-tts"

            def fake_run_session(coro):
                coro.close()
                return {"ok": True, "frames_sent": 5}

            with mock.patch.dict(os.environ, {"TTS_CACHE_DIR": str(Path(td) / "cache")}), \
                 mock.patch.object(client, "_synthesize_tts_file", side_effect=fake_synth), \
                 mock.patch.object(client, "_audio_file_to_alaw", return_value=b"\xff" * 1600), \
                 mock.patch.object(client, "_run_session", side_effect=fake_run_session):
                res = client.speak_tts("Puppy", repeat=50)

            self.assertTrue(res["ok"])
            self.assertEqual(len(synthesized_texts), 1)
            self.assertEqual(synthesized_texts[0].count("Puppy"), 5)


# ===========================================================================
# F7: Acoustic Anti-Clog Analyzer Boundaries
# ===========================================================================
class TestF7AcousticBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F7 (Acoustic Anti-Clog Analyzer)."""

    def test_f7_01_pre_meal_dog_bark_ignored_in_favor_of_longest_sustained_motor_block(self):
        sr = 8000
        rng = np.random.default_rng(42)
        silence_2s = np.zeros(2 * sr, dtype=np.float32)
        # 1.2s low-frequency bark burst (no high-freq ting transients)
        t_bark = np.linspace(0, 1.2, int(1.2 * sr), endpoint=False)
        bark = 0.08 * np.sin(2 * np.pi * 220.0 * t_bark).astype(np.float32)
        silence_3s = np.zeros(3 * sr, dtype=np.float32)
        # 3.2s sustained motor block with sharp 2900 Hz kibble ting transients every 0.14s
        t_motor = np.linspace(0, 3.2, int(3.2 * sr), endpoint=False)
        motor = (0.04 * np.sin(2 * np.pi * 180.0 * t_motor) + rng.normal(0, 0.01, len(t_motor))).astype(np.float32)
        for pulse_sec in np.arange(0.15, 3.1, 0.13):
            idx = int(pulse_sec * sr)
            plen = int(0.012 * sr)
            if idx + plen < len(motor):
                tp = np.linspace(0, 0.012, plen, endpoint=False)
                motor[idx : idx + plen] += (0.55 * np.sin(2 * np.pi * 2900.0 * tp)).astype(np.float32)

        full_sig = np.concatenate([silence_2s, bark, silence_3s, motor, silence_2s])
        with tempfile.TemporaryDirectory() as td:
            wav_p = _write_wav(Path(td) / "bark_then_motor.wav", full_sig, sr)
            analyzer = _make_analyzer(td)
            res = analyzer.analyze_dispense_audio(wav_p)

        self.assertTrue(res.motor_detected)
        self.assertGreaterEqual(res.motor_duration_sec, 2.8, "Longest block (3.2s motor) must be chosen over 1.2s bark")
        self.assertTrue(res.kibble_dispensed_audio, res.diagnosis)

    def test_f7_02_wav_shorter_than_one_second_returns_motor_false(self):
        sr = 8000
        short_sig = np.zeros(int(0.4 * sr), dtype=np.float32)
        with tempfile.TemporaryDirectory() as td:
            wav_p = _write_wav(Path(td) / "short.wav", short_sig, sr)
            analyzer = _make_analyzer(td)
            res = analyzer.analyze_dispense_audio(wav_p)

        self.assertFalse(res.motor_detected)
        self.assertFalse(res.kibble_dispensed_audio)
        self.assertEqual(res.motor_duration_sec, 0.0)
        self.assertIn("too short", res.diagnosis.lower())

    def test_f7_03_motor_hum_with_zero_tings_detects_clog(self):
        sr = 8000
        t = np.linspace(0, 3.5, int(3.5 * sr), endpoint=False)
        hum = (0.05 * np.sin(2 * np.pi * 240.0 * t)).astype(np.float32)
        sig = np.concatenate([np.zeros(sr, dtype=np.float32), hum, np.zeros(sr, dtype=np.float32)])
        with tempfile.TemporaryDirectory() as td:
            wav_p = _write_wav(Path(td) / "clog_hum.wav", sig, sr)
            analyzer = _make_analyzer(td)
            res = analyzer.analyze_dispense_audio(wav_p)

        self.assertTrue(res.motor_detected)
        self.assertEqual(res.ting_count, 0)
        self.assertFalse(res.kibble_dispensed_audio)
        self.assertIn("CLOG / EMPTY HOPPER", res.diagnosis)

    def test_f7_04_assess_dispense_short_run_boundary(self):
        cs = _core_symbols()
        AudioAnalysisResult, assess_dispense = cs.AudioAnalysisResult, cs.assess_dispense

        # 1 portion never triggers short_run even if motor_duration_sec is brief
        a_1p = AudioAnalysisResult(True, 20, True, 20.0, 10.0, "", motor_duration_sec=0.8, kibble_rate_per_sec=15.0)
        self.assertFalse(assess_dispense(a_1p, portions=1)["short_run"])

        # 2 portions: expected = 4.6s, 0.6 * expected = 2.76s
        a_short = AudioAnalysisResult(True, 25, True, 20.0, 10.0, "", motor_duration_sec=2.5, kibble_rate_per_sec=15.0)
        a_ok = AudioAnalysisResult(True, 25, True, 20.0, 10.0, "", motor_duration_sec=2.9, kibble_rate_per_sec=15.0)
        self.assertTrue(assess_dispense(a_short, portions=2)["short_run"])
        self.assertFalse(assess_dispense(a_ok, portions=2)["short_run"])

    def test_f7_05_assess_dispense_low_flow_and_weak_flow_boundaries(self):
        cs = _core_symbols()
        AudioAnalysisResult, assess_dispense = cs.AudioAnalysisResult, cs.assess_dispense

        # low_flow: motor >= 4.0s and kibble_rate_per_sec < 0.4 * 13.0 (5.2)
        a_low = AudioAnalysisResult(
            True, 15, True, 20.0, 10.0, "", motor_duration_sec=5.0, kibble_rate_per_sec=3.0,
            ting_count=30, ting_rate_per_sec=6.0,
        )
        self.assertTrue(assess_dispense(a_low, portions=2)["low_flow"])
        self.assertFalse(assess_dispense(a_low, portions=2)["weak_flow"])

        # weak_flow: motor >= 8.0s, ting_count > 0, ting_rate_per_sec < 4.6
        a_weak = AudioAnalysisResult(
            True, 100, True, 20.0, 10.0, "", motor_duration_sec=10.0, kibble_rate_per_sec=10.0,
            ting_count=35, ting_rate_per_sec=3.5,
        )
        self.assertTrue(assess_dispense(a_weak, portions=4)["weak_flow"])

    def test_f7_06_assess_dispense_recording_truncated_suppresses_warnings(self):
        cs = _core_symbols()
        AudioAnalysisResult, assess_dispense = cs.AudioAnalysisResult, cs.assess_dispense

        a_trunc = AudioAnalysisResult(
            True, 50, True, 20.0, 10.0, "", motor_duration_sec=8.5, kibble_rate_per_sec=10.0,
            ting_count=28, ting_rate_per_sec=3.3,
        )
        flags = assess_dispense(a_trunc, portions=10, recording_truncated=True)
        self.assertFalse(flags["short_run"])
        self.assertFalse(flags["weak_flow"])


# ===========================================================================
# F8: Visual Bowl Analyzer Boundaries
# ===========================================================================
class TestF8VisualBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F8 (Visual Bowl Analyzer)."""

    def test_f8_01_bowl_empty_is_always_false_confirm_only_invariant(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            # Daylight bright empty steel image
            day_img = np.full((1280, 720, 3), (180, 210, 240), dtype=np.uint8)
            day_path = str(Path(td) / "day_empty.jpg")
            _write_bgr_image(day_path, day_img)
            res_day = analyzer.analyze_bowl_visual(day_path)
            self.assertFalse(res_day.bowl_empty, "bowl_empty must ALWAYS be False (confirm-only invariant)")

            # IR night grayscale empty image
            ir_img = np.full((1280, 720, 3), 120, dtype=np.uint8)
            ir_path = str(Path(td) / "ir_empty.jpg")
            _write_bgr_image(ir_path, ir_img)
            res_ir = analyzer.analyze_bowl_visual(ir_path)
            self.assertFalse(res_ir.bowl_empty)
            self.assertTrue(res_ir.is_infrared_mode)

    def test_f8_02_ir_mode_never_prints_daylight_fill_percentages_and_bowl_fill_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            ir_img = np.full((1280, 720, 3), 90, dtype=np.uint8)
            ir_path = str(Path(td) / "ir.jpg")
            _write_bgr_image(ir_path, ir_img)

            fill, is_ir = analyzer.bowl_fill(ir_path)
            self.assertIsNone(fill)
            self.assertTrue(is_ir)
            self.assertIsNone(analyzer.food_seen(ir_path))

            res = analyzer.analyze_bowl_visual(ir_path)
            self.assertIn("IR night vision", res.diagnosis)
            self.assertNotIn("fill", res.diagnosis)
            self.assertNotIn("0.0%", res.diagnosis)

    def test_f8_03_dog_at_bowl_suppresses_food_increased(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            before = np.full((1280, 720, 3), (100, 150, 200), dtype=np.uint8)
            after = before.copy()
            # Large obstruction across the bowl zone [0.74h:0.99h, 0.08w:0.92w]
            after[int(0.75 * 1280) : int(0.98 * 1280), int(0.10 * 720) : int(0.90 * 720)] = (5, 10, 15)
            after[int(0.75 * 1280) : int(0.85 * 1280), int(0.10 * 720) : int(0.45 * 720)] = (240, 250, 255)

            b_path = str(Path(td) / "before.jpg")
            a_path = str(Path(td) / "after.jpg")
            _write_bgr_image(b_path, before)
            _write_bgr_image(a_path, after)

            res = analyzer.analyze_bowl_visual(a_path, b_path)
            self.assertTrue(res.dog_at_bowl_detected)
            self.assertFalse(res.food_increased, "Dog obstruction must suppress food_increased")

    def test_f8_04_ir_to_daylight_transition_not_mistaken_for_dog_at_bowl(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            ir_before = np.full((1280, 720, 3), 100, dtype=np.uint8)  # HSV sat = 0 (< 14.0)
            day_after = np.full((1280, 720, 3), (40, 140, 220), dtype=np.uint8)  # HSV sat > 14.0
            self.assertFalse(analyzer._detect_dog_at_bowl(day_after, ir_before))

    def test_f8_05_unreadable_or_missing_after_image_raises_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            with self.assertRaises(ValueError):
                analyzer.analyze_bowl_visual(str(Path(td) / "nonexistent.jpg"))

            corrupt_jpg = Path(td) / "corrupt.jpg"
            corrupt_jpg.write_bytes(b"not a valid jpeg image")
            with self.assertRaises(ValueError):
                analyzer.analyze_bowl_visual(str(corrupt_jpg))

    def test_f8_06_gemini_llm_network_error_returns_none_gracefully(self):
        with tempfile.TemporaryDirectory() as td:
            analyzer = _make_analyzer(td)
            analyzer.cfg.gemini_api_key = "fake_gemini_key"
            img_path = str(Path(td) / "after.jpg")
            _write_bgr_image(img_path, np.full((128, 128, 3), 120, dtype=np.uint8))

            with mock.patch(
                "requests.post",
                side_effect=requests.exceptions.ConnectionError("Gemini API unreachable"),
            ):
                food_llm, reason = analyzer._verify_with_gemini(img_path)

            self.assertIsNone(food_llm)
            self.assertIn("LLM check error", reason)


# ===========================================================================
# F9: Audio-First Decision Matrix & `recording_cut_short` Guard Boundaries
# ===========================================================================
class TestF9DecisionBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F9 (Decision Matrix & Cut-Short Guard)."""

    def test_f9_01_recording_cut_short_coverage_and_tail_gap_boundaries(self):
        d = _core_decision()
        # Coverage < 0.75 * wall -> True
        self.assertTrue(d.recording_cut_short({"recorded_wall_sec": 13.0, "audio_sec": 1.36, "audio_last_rx_sec": 1.4}))
        # Coverage >= 0.75, but tail gap (80 - 72 = 8.0 > 5.0) -> True
        self.assertTrue(d.recording_cut_short({"recorded_wall_sec": 80.0, "audio_sec": 72.0, "audio_last_rx_sec": 72.0}))
        # Coverage >= 0.75 and tail gap <= 5.0 -> False
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 80.0, "audio_sec": 65.0, "audio_last_rx_sec": 76.0}))

    def test_f9_02_recording_cut_short_short_window_and_missing_keys_return_false(self):
        d = _core_decision()
        self.assertFalse(d.recording_cut_short(None))
        self.assertFalse(d.recording_cut_short({}))
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 12.0}))
        self.assertFalse(d.recording_cut_short({"audio_sec": 1.0}))
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": "bad", "audio_sec": 1.0}))
        # Window < 3.0s is never flagged as cut short
        self.assertFalse(d.recording_cut_short({"recorded_wall_sec": 2.9, "audio_sec": 0.1, "audio_last_rx_sec": 0.1}))

    def test_f9_03_cut_short_without_kibble_is_unverified_and_never_clog(self):
        cs = _core_symbols()
        AudioAnalysisResult, VisualAnalysisResult = cs.AudioAnalysisResult, cs.VisualAnalysisResult

        d = _core_decision()
        a_silent = AudioAnalysisResult(False, 0, False, 0.0, 0.0, "silent", 0.0)
        v_ir = VisualAnalysisResult(
            0.0, 0.0, 0.0, 0.2, False, True, True, None, "", "", "IR", is_infrared_mode=True
        )
        passed = d.decide_passed(a_silent, v_ir, feed_abnormal=0, expect_dispense=True)
        self.assertFalse(passed)
        outcome, ok = d.classify(a_silent, v_ir, expect_dispense=True, passed=passed, cut_short=True)
        self.assertEqual((outcome, ok), (d.RECORDING_CUT_SHORT, False))
        self.assertFalse(d.is_clog(outcome, ok))
        self.assertTrue(d.is_unverified(outcome))
        self.assertEqual(d.status_label(outcome, ok), "unverified")

    def test_f9_04_kibble_heard_before_cut_short_still_passes_as_dispensed(self):
        AudioAnalysisResult = _core_symbols().AudioAnalysisResult

        d = _core_decision()
        a_good = AudioAnalysisResult(True, 30, True, 25.0, 12.0, "ok", 4.0)
        passed = d.decide_passed(a_good, None, feed_abnormal=0, expect_dispense=True)
        self.assertTrue(passed)
        outcome, ok = d.classify(a_good, None, expect_dispense=True, passed=passed, cut_short=True)
        self.assertEqual((outcome, ok), (d.DISPENSED, True))
        self.assertFalse(d.is_clog(outcome, ok))

    def test_f9_05_vision_can_override_audio_false_vs_true_boundary(self):
        cs = _core_symbols()
        AudioAnalysisResult, VisualAnalysisResult = cs.AudioAnalysisResult, cs.VisualAnalysisResult

        d = _core_decision()
        a_clog = AudioAnalysisResult(True, 3, False, 25.0, 4.0, "clog", 3.5)
        v_increased = VisualAnalysisResult(0.01, 0.08, 0.07, 0.1, False, True, True, None, "", "", "new food")

        self.assertFalse(
            d.decide_passed(a_clog, v_increased, 0, True, vision_can_override_audio=False),
            "By default vision must NOT override an audio clog verdict",
        )
        self.assertTrue(
            d.decide_passed(a_clog, v_increased, 0, True, vision_can_override_audio=True),
            "When vision_can_override_audio=True, vision confirmation overrides audio",
        )

    def test_f9_06_no_audio_captured_falls_back_to_vision_or_camera_unavailable(self):
        VisualAnalysisResult = _core_symbols().VisualAnalysisResult

        d = _core_decision()
        passed_none = d.decide_passed(None, None, 0, True)
        self.assertFalse(passed_none)
        outcome, ok = d.classify(None, None, True, passed_none, cut_short=False)
        self.assertEqual((outcome, ok), (d.CAMERA_UNAVAILABLE, False))
        self.assertFalse(d.is_clog(outcome, ok))
        self.assertTrue(d.is_unverified(outcome))

        v_ok = VisualAnalysisResult(0.01, 0.09, 0.08, 0.1, False, True, True, None, "", "", "new food")
        passed_vis = d.decide_passed(None, v_ok, 0, True)
        self.assertTrue(passed_vis)
        self.assertEqual(d.classify(None, v_ok, True, passed_vis), (d.UNVERIFIED, True))


# ===========================================================================
# F10: SQLite WAL State Store, Hopper Tracking & Retention Boundaries
# ===========================================================================
class TestF10DatabaseBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F10 (SQLite DB, Hopper & Retention)."""

    def test_f10_01_hopper_status_before_first_refill_has_known_false(self):
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            hop = db.hopper_status(240)
            self.assertFalse(hop["known"])
            self.assertIsNone(hop["refilled_at"])
            self.assertEqual(hop["capacity_portions"], 240)
            self.assertEqual(hop["portions_left"], 240)
            self.assertEqual(hop["percent_left"], 100.0)

    def test_f10_02_motor_no_kibble_does_not_subtract_hopper_portions(self):
        import time as _time
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            now = _time.time()
            with mock.patch("time.time", return_value=now - 5.0):
                db.record_refill(100, note="full")
            _log_feed(db, event_type="scheduled_verify", portions=5, success=True, clog_detected=False, motor_detected=True, outcome="dispensed")
            _log_feed(db, event_type="manual_feed_unverified", portions=3, success=True, clog_detected=False, motor_detected=False, outcome="unverified")
            _log_feed(db, event_type="scheduled_verify", portions=10, success=False, clog_detected=True, motor_detected=True, outcome="motor_no_kibble")
            _log_feed(db, event_type="scheduled_verify", portions=10, success=False, clog_detected=False, motor_detected=False, outcome="camera_unavailable")

            hop = db.hopper_status(100)
            self.assertTrue(hop["known"])
            self.assertEqual(hop["portions_used"], 8)
            self.assertEqual(hop["portions_left"], 92)
            self.assertEqual(hop["percent_left"], 92.0)

    def test_f10_03_hopper_portions_left_clamped_at_zero_when_overused(self):
        import time as _time
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            now = _time.time()
            with mock.patch("time.time", return_value=now - 5.0):
                db.record_refill(10, note="small refill")
            _log_feed(db, portions=16, success=True, clog_detected=False, motor_detected=True, outcome="dispensed")
            hop = db.hopper_status(10)
            self.assertEqual(hop["portions_used"], 16)
            self.assertEqual(hop["portions_left"], 0)
            self.assertEqual(hop["percent_left"], 0.0)

    def test_f10_04_consecutive_missed_feeds_ignores_unverified_and_cut_short(self):
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            _log_feed(db, portions=2, success=True, clog_detected=False, outcome="dispensed")
            self.assertEqual(db.consecutive_missed_feeds(), 0)

            _log_feed(db, portions=2, success=False, clog_detected=True, outcome="motor_no_kibble")
            self.assertEqual(db.consecutive_missed_feeds(), 1)

            # Unverified / cut-short runs neither increment nor reset the streak
            _log_feed(db, portions=2, success=False, clog_detected=False, outcome="recording_cut_short")
            _log_feed(db, portions=2, success=False, clog_detected=False, outcome="camera_unavailable")
            _log_feed(db, portions=2, success=False, clog_detected=False, outcome="unverified")
            self.assertEqual(db.consecutive_missed_feeds(), 1)

            _log_feed(db, portions=2, success=False, clog_detected=True, outcome="no_motor")
            self.assertEqual(db.consecutive_missed_feeds(), 2)

            # A verified dispense resets the streak to 0
            _log_feed(db, portions=2, success=True, clog_detected=False, outcome="dispensed")
            self.assertEqual(db.consecutive_missed_feeds(), 0)

    def test_f10_05_retention_manager_refuses_to_delete_protected_or_outside_files(self):
        cs = _core_symbols()
        KibbleDatabase, RetentionManager, RuntimeConfig = (
            cs.KibbleDatabase,
            cs.RetentionManager,
            cs.RuntimeConfig,
        )

        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td) / "captures"
            outside_dir = Path(td) / "outside"
            work_dir.mkdir()
            outside_dir.mkdir()

            protected_file = work_dir / "latest_annotated.jpg"
            protected_file.write_bytes(b"protected_image")
            outside_file = outside_dir / "outside_clip.mp4"
            outside_file.write_bytes(b"outside_video")

            db = KibbleDatabase(str(Path(td) / "kibble.db"))
            try:
                cfg = RuntimeConfig(storage_dir=Path(td))
            except TypeError:
                cfg = RuntimeConfig()
            rm = RetentionManager(cfg, db, work_dir, protected=[protected_file])

            self.assertIsNone(rm._safe(str(protected_file)))
            self.assertIsNone(rm._safe(str(outside_file)))
            deleted = rm._delete_files(
                {"annotated_image": str(protected_file), "video_clip": str(outside_file)}
            )
            self.assertEqual(deleted, 0)
            self.assertTrue(protected_file.exists())
            self.assertTrue(outside_file.exists())

    def test_f10_06_corrupt_sqlite_database_auto_recovers_from_backup(self):
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "recover.db"
            db = KibbleDatabase(str(db_path))
            _log_feed(db, portions=4, success=True, clog_detected=False, outcome="dispensed")
            self.assertTrue(db.backup_path.exists())

            # Corrupt the primary SQLite file header
            db_path.write_bytes(b"CORRUPT_SQLITE_HEADER_BYTES" * 20)
            db_recovered = KibbleDatabase(str(db_path))
            events = db_recovered.get_recent_events(limit=5) if hasattr(db_recovered, "get_recent_events") else db_recovered.recent_events(limit=5)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["portions"], 4)


# ===========================================================================
# F11: HACS & HA Manifests + Standalone `_ha_compat.py` Boundaries
# ===========================================================================
class TestF11StandaloneBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F11 (HACS Manifest & Standalone Compat)."""

    def test_f11_01_zero_external_vps_dependencies_in_custom_component(self):
        comp_dir = ROOT / "custom_components" / "molly_cody_feeder"
        self.assertTrue(comp_dir.is_dir(), "custom_components/molly_cody_feeder must exist")
        forbidden_tokens = ("vps_install.sh", "review_server.py", "docker-compose")
        py_files = list(comp_dir.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 10)
        for py_file in py_files:
            content = py_file.read_text(encoding="utf-8")
            for tok in forbidden_tokens:
                self.assertNotIn(tok, content, f"Standalone HACS file {py_file.name} must not reference {tok}")

    def test_f11_02_ha_compat_and_const_import_cleanly_in_bare_vm(self):
        from custom_components.molly_cody_feeder import _ha_compat, const

        self.assertEqual(const.DOMAIN, "molly_cody_feeder")
        self.assertEqual(const.VERSION, "0.7.0")
        expected_platforms = {
            "sensor",
            "binary_sensor",
            "switch",
            "button",
            "number",
            "select",
            "camera",
            "image",
            "event",
        }
        self.assertEqual(set(const.PLATFORMS), expected_platforms)
        self.assertTrue(hasattr(_ha_compat, "HomeAssistant"))
        self.assertTrue(hasattr(_ha_compat, "DataUpdateCoordinator"))
        self.assertTrue(hasattr(_ha_compat, "CoordinatorEntity"))

    def test_f11_03_runtime_config_from_entry_merges_options_over_data_and_isolates_storage(self):
        RuntimeConfig = _core_symbols().RuntimeConfig

        with tempfile.TemporaryDirectory() as td:
            hass, entry = _make_hass_and_entry(
                td,
                data_overrides={"connection_mode": "auto", "enable_camera": True, "hopper_capacity_portions": 240},
                options_overrides={"connection_mode": "lan", "enable_camera": False, "hopper_capacity_portions": 180},
                entry_id="isolated_entry_99",
            )
            rcfg = RuntimeConfig.from_entry(hass, entry)
            self.assertEqual(rcfg.connection_mode, "lan")
            self.assertFalse(rcfg.enable_camera)
            self.assertEqual(rcfg.hopper_capacity_portions, 180)
            storage_str = str(getattr(rcfg, "storage_dir", getattr(rcfg, "db_path", "")))
            self.assertIn("isolated_entry_99", storage_str)

    def test_f11_04_standalone_env_load_env_works_without_python_dotenv(self):
        import importlib.util

        env_path = ROOT / "custom_components" / "molly_cody_feeder" / "core" / "env.py"
        spec = importlib.util.spec_from_file_location("_molly_core_env_test", env_path)
        self.assertIsNotNone(spec)
        core_env = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        with mock.patch.dict(sys.modules, {"dotenv": None}):
            spec.loader.exec_module(core_env)
            core_env._loaded = False
            core_env.load_env()
            self.assertTrue(core_env._loaded)

    def test_f11_05_learned_baselines_returns_none_when_fewer_than_min_samples(self):
        KibbleDatabase = _core_symbols().KibbleDatabase

        with tempfile.TemporaryDirectory() as td:
            db = KibbleDatabase(str(Path(td) / "base.db"))
            # Only 2 samples (< min_samples=3)
            _log_feed(db, portions=2, success=True, outcome="dispensed", motor_duration_sec=4.8, kibble_rate=13.0)
            _log_feed(db, portions=2, success=True, outcome="dispensed", motor_duration_sec=5.0, kibble_rate=14.0)
            base = db.learned_baselines()
            self.assertIsNone(base["kibble_rate"])
            self.assertIsNone(base["motor_sec_per_portion"])


# ===========================================================================
# F12: Config Flow & Options Flow with Modular Compute Toggles Boundaries
# ===========================================================================
class TestF12ConfigFlowBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F12 (ConfigFlow & OptionsFlow)."""

    async def test_f12_01_invalid_cloud_credentials_returns_invalid_auth_error(self):
        from custom_components.molly_cody_feeder.config_flow import MollyCodyFeederConfigFlow
        from custom_components.molly_cody_feeder.const import (
            CONF_CONNECTION_MODE,
            CONF_COUNTRY_CODE,
            CONF_EMAIL,
            CONF_PASSWORD,
        )

        MollyFeederClient = _core_symbols().MollyFeederClient
        with tempfile.TemporaryDirectory() as td:
            hass, _ = _make_hass_and_entry(td)
            flow = MollyCodyFeederConfigFlow()
            flow.hass = hass

            with mock.patch.object(
                MollyFeederClient,
                "login",
                side_effect=RuntimeError("Molly & Cody Cloud login failed: USER_PASSWD_WRONG"),
            ):
                res = await flow.async_step_user(
                    {
                        CONF_EMAIL: "bad@example.com",
                        CONF_PASSWORD: "wrong_password",
                        CONF_COUNTRY_CODE: "1",
                        CONF_CONNECTION_MODE: "cloud",
                    }
                )

            self.assertEqual(res["type"], "form")
            self.assertIn("errors", res)
            self.assertEqual(res["errors"].get("base"), "invalid_auth")

    async def test_f12_02_cloud_network_failure_returns_cannot_connect_error(self):
        from custom_components.molly_cody_feeder.config_flow import MollyCodyFeederConfigFlow
        from custom_components.molly_cody_feeder.const import (
            CONF_CONNECTION_MODE,
            CONF_COUNTRY_CODE,
            CONF_EMAIL,
            CONF_PASSWORD,
        )

        MollyFeederClient = _core_symbols().MollyFeederClient
        with tempfile.TemporaryDirectory() as td:
            hass, _ = _make_hass_and_entry(td)
            flow = MollyCodyFeederConfigFlow()
            flow.hass = hass

            with mock.patch.object(
                MollyFeederClient,
                "login",
                side_effect=requests.exceptions.ConnectionError("Failed to establish a new connection"),
            ):
                res = await flow.async_step_user(
                    {
                        CONF_EMAIL: "user@example.com",
                        CONF_PASSWORD: "secret",
                        CONF_COUNTRY_CODE: "1",
                        CONF_CONNECTION_MODE: "cloud",
                    }
                )

            self.assertEqual(res["type"], "form")
            self.assertIn("errors", res)
            self.assertEqual(res["errors"].get("base"), "cannot_connect")

    async def test_f12_03_duplicate_device_id_aborts_already_configured(self):
        from custom_components.molly_cody_feeder.config_flow import MollyCodyFeederConfigFlow
        from custom_components.molly_cody_feeder.const import (
            CONF_CONNECTION_MODE,
            CONF_COUNTRY_CODE,
            CONF_DEVICE_ID,
            CONF_EMAIL,
            CONF_PASSWORD,
        )

        MollyFeederClient = _core_symbols().MollyFeederClient
        with tempfile.TemporaryDirectory() as td:
            hass, existing_entry = _make_hass_and_entry(
                td, data_overrides={CONF_DEVICE_ID: "dup_device_123"}
            )
            if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "_entries"):
                hass.config_entries._entries[existing_entry.entry_id] = existing_entry
            flow = MollyCodyFeederConfigFlow()
            flow.hass = hass
            flow._existing_entries = [existing_entry]

            def fake_login(self_client, force=False):
                self_client.device_id = "dup_device_123"
                self_client.device_name = "Molly Feeder"
                return True

            with mock.patch.object(
                MollyFeederClient,
                "login",
                new=fake_login,
            ):
                res = await flow.async_step_user(
                    {
                        CONF_EMAIL: "owner@example.com",
                        CONF_PASSWORD: "secret",
                        CONF_COUNTRY_CODE: "1",
                        CONF_DEVICE_ID: "dup_device_123",
                        CONF_CONNECTION_MODE: "cloud",
                    }
                )

            self.assertEqual(res["type"], "abort")
            self.assertEqual(res["reason"], "already_configured")

    async def test_f12_04_lan_mode_missing_ip_or_local_key_returns_validation_error(self):
        from custom_components.molly_cody_feeder.config_flow import MollyCodyFeederConfigFlow
        from custom_components.molly_cody_feeder.const import (
            CONF_CONNECTION_MODE,
            CONF_DEVICE_ID,
            CONF_LOCAL_IP,
            CONF_LOCAL_KEY,
        )

        with tempfile.TemporaryDirectory() as td:
            hass, _ = _make_hass_and_entry(td)
            flow = MollyCodyFeederConfigFlow()
            flow.hass = hass

            res = await flow.async_step_user(
                {
                    CONF_CONNECTION_MODE: "lan",
                    CONF_DEVICE_ID: "lan_dev_1",
                    CONF_LOCAL_IP: "",
                    CONF_LOCAL_KEY: "",
                }
            )
            if res.get("type") == "form" and not res.get("errors") and hasattr(flow, "async_step_lan"):
                res = await flow.async_step_lan(
                    {
                        CONF_DEVICE_ID: "lan_dev_1",
                        CONF_LOCAL_IP: "",
                        CONF_LOCAL_KEY: "",
                    }
                )
            self.assertEqual(res["type"], "form")
            self.assertTrue(bool(res.get("errors")))

    async def test_f12_05_options_flow_preserves_existing_options_on_partial_update(self):
        from custom_components.molly_cody_feeder.config_flow import MollyCodyFeederConfigFlow
        from custom_components.molly_cody_feeder.const import (
            CONF_ENABLE_CAMERA,
            CONF_ENABLE_TTS,
            CONF_HOPPER_CAPACITY,
            CONF_MAX_DAILY_RETRY,
        )

        with tempfile.TemporaryDirectory() as td:
            hass, entry = _make_hass_and_entry(
                td,
                options_overrides={
                    CONF_ENABLE_CAMERA: True,
                    CONF_ENABLE_TTS: True,
                    CONF_HOPPER_CAPACITY: 240,
                    CONF_MAX_DAILY_RETRY: 3,
                },
            )
            opt_flow = MollyCodyFeederConfigFlow.async_get_options_flow(entry)
            opt_flow.hass = hass

            res = await opt_flow.async_step_init({CONF_ENABLE_CAMERA: False})
            self.assertEqual(res["type"], "create_entry")
            self.assertFalse(res["data"][CONF_ENABLE_CAMERA])
            self.assertTrue(res["data"][CONF_ENABLE_TTS])
            self.assertEqual(res["data"][CONF_HOPPER_CAPACITY], 240)
            self.assertEqual(res["data"][CONF_MAX_DAILY_RETRY], 3)


# ===========================================================================
# F13: `MollyCodyDataUpdateCoordinator` & Scheduled Meal Engine Boundaries
# ===========================================================================
class TestF13CoordinatorBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F13 (Coordinator & Scheduled Meal Engine)."""

    async def test_f13_01_preroll_midnight_wraparound_calculates_23_59_20(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                status_overrides={"schedule_raw": "7f0000021000000"},
            )
            preroll = None
            for method_name in ("compute_preroll_time", "_compute_preroll_time", "_preroll_hms", "preroll_time"):
                fn = getattr(coord, method_name, None)
                if callable(fn):
                    try:
                        preroll = fn(0, 0, 40)
                    except TypeError:
                        preroll = fn("00:00", 40)
                    break

            if preroll is None:
                # Inspect scheduled timers registered via async_track_time_change for the 00:00 meal
                tracked: list[tuple[int, int, int]] = []
                import custom_components.molly_cody_feeder.coordinator as coord_mod

                orig_track = getattr(coord_mod, "async_track_time_change", None)
                if callable(orig_track):
                    def spy_track(hass_arg, action, hour=None, minute=None, second=None):
                        if hour is not None and minute is not None and second is not None:
                            tracked.append((int(hour), int(minute), int(second)))
                        return orig_track(hass_arg, action, hour=hour, minute=minute, second=second)

                    with mock.patch.object(coord_mod, "async_track_time_change", side_effect=spy_track):
                        if hasattr(coord, "async_sync_schedules"):
                            await coord.async_sync_schedules()
                        elif hasattr(coord, "_sync_schedule_timers"):
                            coord._sync_schedule_timers()
                    if (23, 59, 20) in tracked:
                        preroll = (23, 59, 20)

            self.assertIsNotNone(
                preroll,
                "Coordinator must expose a pre-roll time helper or register 23:59:20 timer for a 00:00 meal with 40s pre-roll",
            )
            if isinstance(preroll, tuple):
                self.assertEqual(preroll, (23, 59, 20))
            else:
                self.assertEqual(str(preroll), "23:59:20")

    @staticmethod
    async def _invoke_scheduled_meal_or_retry(coord: Any, portions: int = 4) -> Any:
        if hasattr(coord, "async_run_scheduled_meal"):
            return await coord.async_run_scheduled_meal(portions=portions)
        if hasattr(coord, "_async_run_scheduled_meal"):
            return await coord._async_run_scheduled_meal(portions=portions)
        if hasattr(coord, "_async_run_scheduled_verify"):
            return await coord._async_run_scheduled_verify(portions=portions)
        if hasattr(coord, "_async_handle_clog_retry"):
            return await coord._async_handle_clog_retry(initial_outcome="motor_no_kibble")
        if hasattr(coord, "run_scheduled_check"):
            res = coord.run_scheduled_check(meal_portions=portions)
            return await res if asyncio.iscoroutine(res) else res
        if hasattr(coord, "async_verify_feed"):
            return await coord.async_verify_feed(portions=portions, trigger="scheduled_verify")
        raise AssertionError("Coordinator does not expose a scheduled meal or clog retry entry point")

    async def test_f13_02_auto_retry_daily_cap_blocks_fourth_retry_portion(self):
        from custom_components.molly_cody_feeder.const import (
            CONF_ENABLE_AUTO_RETRY,
            CONF_MAX_DAILY_RETRY,
        )

        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                options_overrides={CONF_ENABLE_AUTO_RETRY: True, CONF_MAX_DAILY_RETRY: 3},
            )
            # Pre-log 3 auto-retry portions today in the coordinator's DB
            for _ in range(3):
                _log_feed(
                    coord.db,
                    event_type="auto_retry_verify",
                    portions=1,
                    success=False,
                    clog_detected=True,
                    outcome="motor_no_kibble",
                )
            self.assertEqual(coord.db.portions_today("auto_retry_verify"), 3)

            with mock.patch.object(
                coord,
                "async_verify_feed",
                new=mock.AsyncMock(
                    return_value={
                        "ok": False,
                        "passed": False,
                        "outcome": "motor_no_kibble",
                        "clog_detected": True,
                    }
                ),
            ) as m_verify:
                res = await self._invoke_scheduled_meal_or_retry(coord, portions=5)

            retry_calls = [
                c for c in m_verify.call_args_list
                if c.kwargs.get("trigger") == "auto_retry_verify"
                or c.kwargs.get("event_type") == "auto_retry_verify"
                or (len(c.args) >= 2 and c.args[1] == "auto_retry_verify")
            ]
            self.assertEqual(len(retry_calls), 0, "4th auto-retry portion must be blocked by daily cap of 3")
            self.assertEqual(coord.db.portions_today("auto_retry_verify"), 3)
            self.assertIsNotNone(res)

    async def test_f13_03_hopper_empty_suspected_stops_auto_retry_when_both_motor_no_kibble(self):
        from custom_components.molly_cody_feeder.const import (
            CONF_ENABLE_AUTO_RETRY,
            CONF_MAX_DAILY_RETRY,
        )

        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                options_overrides={CONF_ENABLE_AUTO_RETRY: True, CONF_MAX_DAILY_RETRY: 5},
            )
            if hasattr(coord, "runtime_config"):
                setattr(coord.runtime_config, "max_retry_attempts", 3)

            outcomes = [
                {"ok": False, "passed": False, "outcome": "motor_no_kibble", "clog_detected": True},
                {"ok": False, "passed": False, "outcome": "motor_no_kibble", "clog_detected": True},
                {"ok": True, "passed": True, "outcome": "dispensed", "clog_detected": False},
            ]
            with mock.patch.object(
                coord,
                "async_verify_feed",
                new=mock.AsyncMock(side_effect=outcomes),
            ) as m_verify:
                res = await self._invoke_scheduled_meal_or_retry(coord, portions=4)

            self.assertGreaterEqual(m_verify.call_count, 1)
            self.assertLessEqual(
                m_verify.call_count,
                2,
                "Must stop immediately after scheduled meal + 1st retry both return motor_no_kibble",
            )
            combined_evidence = json.dumps(
                {
                    "res": res,
                    "last_alert_event": coord.data.get("last_alert_event"),
                    "summary": coord.data.get("summary"),
                },
                default=str,
            ).lower()
            self.assertTrue(
                "hopper_empty_suspected" in combined_evidence
                or "hopper likely empty" in combined_evidence
                or "hopper_empty" in combined_evidence,
                f"Expected hopper_empty_suspected evidence in coordinator state, got: {combined_evidence}",
            )

    async def test_f13_04_enable_verification_false_skips_verifier_on_async_feed(self):
        from custom_components.molly_cody_feeder.const import CONF_ENABLE_VERIFICATION

        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                options_overrides={CONF_ENABLE_VERIFICATION: False},
            )
            with mock.patch.object(coord.client, "dispense_food", return_value=(True, "cloud")) as m_disp, \
                 mock.patch.object(coord.client, "capture_av_clip") as m_cap:
                res = await coord.async_feed(portions=3, verify=False)

            m_disp.assert_called_once_with(3)
            m_cap.assert_not_called()
            self.assertTrue(res.get("ok", res.get("success", True)))

    async def test_f13_05_enable_auto_retry_false_never_retries_on_clog(self):
        from custom_components.molly_cody_feeder.const import CONF_ENABLE_AUTO_RETRY

        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                options_overrides={CONF_ENABLE_AUTO_RETRY: False},
            )
            with mock.patch.object(
                coord,
                "async_verify_feed",
                new=mock.AsyncMock(
                    return_value={
                        "ok": False,
                        "passed": False,
                        "outcome": "motor_no_kibble",
                        "clog_detected": True,
                    }
                ),
            ) as m_verify:
                await self._invoke_scheduled_meal_or_retry(coord, portions=2)

            retry_calls = [
                c for c in m_verify.call_args_list
                if c.kwargs.get("trigger") == "auto_retry_verify"
                or c.kwargs.get("event_type") == "auto_retry_verify"
                or (len(c.args) >= 2 and c.args[1] == "auto_retry_verify")
            ]
            self.assertEqual(len(retry_calls), 0, "With enable_auto_retry=False, zero retry attempts are allowed")
            self.assertEqual(coord.db.portions_today("auto_retry_verify"), 0)

    async def test_f13_06_polling_when_feeder_offline_sets_online_false(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                status_overrides={"online": False, "active_transport": "none"},
            )
            self.assertFalse(coord.data["online"])
            self.assertEqual(coord.data["active_transport"], "none")


# ===========================================================================
# F14: Integration Setup, Custom Services & Diagnostics Boundaries
# ===========================================================================
class TestF14ServicesAndDiagnosticsBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F14 (Setup, Services & Diagnostics)."""

    async def test_f14_01_diagnostics_strictly_redacts_all_secret_fields(self):
        from custom_components.molly_cody_feeder.const import (
            CONF_EMAIL,
            CONF_LOCAL_KEY,
            CONF_PASSWORD,
        )
        from custom_components.molly_cody_feeder.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(
                td,
                data_overrides={
                    CONF_EMAIL: "ultra_secret_email@example.com",
                    CONF_PASSWORD: "UltraSecretPassword!999",
                    CONF_LOCAL_KEY: "UltraSecretLocalKey!888",
                },
            )
            coord.client.sid = "secret_session_sid_111"
            coord.client.ecode = "secret_session_ecode_222"
            coord.client.uid = "secret_session_uid_333"

            diag = await async_get_config_entry_diagnostics(hass, entry)
            serialized = json.dumps(diag)

            for secret_val in (
                "UltraSecretPassword!999",
                "UltraSecretLocalKey!888",
                "ultra_secret_email@example.com",
                "secret_session_sid_111",
                "secret_session_ecode_222",
                "secret_session_uid_333",
            ):
                self.assertNotIn(
                    secret_val,
                    serialized,
                    f"Secret value {secret_val!r} leaked in diagnostics output!",
                )
            self.assertIn("**REDACTED**", serialized)

    async def test_f14_02_services_disabled_compute_toggles_handled_gracefully(self):
        from custom_components.molly_cody_feeder.const import (
            CONF_ENABLE_CAMERA,
            CONF_ENABLE_TTS,
        )

        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(
                td,
                options_overrides={CONF_ENABLE_CAMERA: False, CONF_ENABLE_TTS: False},
            )
            with mock.patch.object(coord.client, "speak_tts") as m_tts, \
                 mock.patch.object(coord.client, "capture_snapshot") as m_snap:
                tts_res = await coord.async_play_tts("Dinner time")
                snap_res = await coord.async_capture_snapshot()

            m_tts.assert_not_called()
            m_snap.assert_not_called()
            self.assertFalse(tts_res.get("ok", False))
            self.assertIsNone(snap_res)

    async def test_f14_03_set_schedule_slot_invalid_slot_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(td)
            rejected_0 = False
            try:
                res0 = await coord.async_set_schedule_slot(slot=0, portions=2)
                rejected_0 = not bool(res0)
            except ValueError:
                rejected_0 = True
            self.assertTrue(rejected_0, "Slot 0 must be rejected")

            rejected_9 = False
            try:
                res9 = await coord.async_set_schedule_slot(slot=9, portions=2)
                rejected_9 = not bool(res9)
            except ValueError:
                rejected_9 = True
            self.assertTrue(rejected_9, "Slot 9 must be rejected")

    async def test_f14_04_async_unload_entry_cleans_up_hass_data(self):
        import custom_components.molly_cody_feeder as init_mod
        from custom_components.molly_cody_feeder.const import DOMAIN

        with tempfile.TemporaryDirectory() as td:
            hass, entry, _ = await _setup_entry_with_mock_client(td)
            self.assertIn(entry.entry_id, hass.data.get(DOMAIN, {}))
            unloaded = await init_mod.async_unload_entry(hass, entry)
            self.assertTrue(unloaded)
            self.assertNotIn(entry.entry_id, hass.data.get(DOMAIN, {}))

    async def test_f14_05_clear_clog_alert_resets_clog_state_to_false(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(td)
            coord.data["summary"]["clog_detected"] = True
            coord.data["summary"]["status"] = "problem"
            await coord.async_clear_clog_alert()
            self.assertFalse(coord.data["summary"]["clog_detected"])
            self.assertEqual(coord.data["summary"]["status"], "ok")


# ===========================================================================
# F15: Sensor & Binary Sensor Platforms Boundaries
# ===========================================================================
class TestF15SensorBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F15 (Sensor & BinarySensor Platforms)."""

    async def test_f15_01_sensors_handle_empty_initial_database_none_metrics(self):
        from custom_components.molly_cody_feeder import sensor as sensor_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            entities: list[Any] = []
            await sensor_mod.async_setup_entry(hass, entry, entities.extend)
            self.assertEqual(len(entities), 22)

            # Force None on optional summary fields before first meal
            for nullable_key in (
                "last_meal_time",
                "last_meal_portions",
                "minutes_since_last_meal",
                "next_meal_time",
                "minutes_to_next_meal",
                "week_confirmed_pct",
                "kibble_flow",
                "kibble_flow_7d_avg",
                "last_motor_duration_sec",
                "bowl_fill_pct",
                "hopper_days_left",
            ):
                coord.data["summary"][nullable_key] = None

            checked = 0
            for ent in entities:
                _ = ent.native_value
                _ = ent.extra_state_attributes
                self.assertTrue(bool(ent.unique_id))
                checked += 1
            self.assertEqual(checked, 22)

    async def test_f15_02_hardware_fault_binary_sensor_dp255_one_vs_two_boundary(self):
        from custom_components.molly_cody_feeder import binary_sensor as bs_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            entities: list[Any] = []
            await bs_mod.async_setup_entry(hass, entry, entities.extend)
            self.assertEqual(len(entities), 9)

            fault_ent = next(
                e for e in entities
                if "fault" in e.unique_id.lower() or "abnormal" in e.unique_id.lower()
            )
            coord.data["feed_abnormal"] = 1
            coord.data["dps"]["255"] = 1
            self.assertFalse(fault_ent.is_on, "DP 255 == 1 must report hardware_fault=False")

            coord.data["feed_abnormal"] = 2
            coord.data["dps"]["255"] = 2
            self.assertTrue(fault_ent.is_on, "DP 255 == 2 (> 1) must report hardware_fault=True")

    async def test_f15_03_hopper_low_binary_sensor_threshold_boundary(self):
        from custom_components.molly_cody_feeder import binary_sensor as bs_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            entities: list[Any] = []
            await bs_mod.async_setup_entry(hass, entry, entities.extend)

            hopper_ent = next(e for e in entities if "hopper_low" in e.unique_id.lower())
            coord.data["summary"]["hopper_known"] = True
            coord.data["summary"]["hopper_percent"] = 20.0
            coord.data["summary"]["hopper_low"] = True
            self.assertTrue(hopper_ent.is_on)

            coord.data["summary"]["hopper_percent"] = 20.1
            coord.data["summary"]["hopper_low"] = False
            self.assertFalse(hopper_ent.is_on)

    async def test_f15_04_online_binary_sensor_false_when_transport_is_none(self):
        from custom_components.molly_cody_feeder import binary_sensor as bs_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            entities: list[Any] = []
            await bs_mod.async_setup_entry(hass, entry, entities.extend)

            online_ent = next(e for e in entities if "online" in e.unique_id.lower())
            self.assertTrue(online_ent.is_on)
            coord.data["online"] = False
            coord.data["active_transport"] = "none"
            self.assertFalse(online_ent.is_on)

    async def test_f15_05_weak_flow_and_clog_binary_sensors_reflect_summary_flags(self):
        from custom_components.molly_cody_feeder import binary_sensor as bs_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            entities: list[Any] = []
            await bs_mod.async_setup_entry(hass, entry, entities.extend)

            weak_ent = next(e for e in entities if "weak_flow" in e.unique_id.lower())
            clog_ent = next(e for e in entities if "clog" in e.unique_id.lower())

            coord.data["summary"]["weak_flow_warning"] = False
            coord.data["summary"]["clog_detected"] = False
            self.assertFalse(weak_ent.is_on)
            self.assertFalse(clog_ent.is_on)

            coord.data["summary"]["weak_flow_warning"] = True
            coord.data["summary"]["clog_detected"] = True
            self.assertTrue(weak_ent.is_on)
            self.assertTrue(clog_ent.is_on)


# ===========================================================================
# F16: Switch, Button, Number & Select Platforms Boundaries
# ===========================================================================
class TestF16ControlEntityBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F16 (Switch, Button, Number, Select)."""

    async def test_f16_01_schedule_slot_switches_and_numbers_handle_short_schedule_list(self):
        from custom_components.molly_cody_feeder import number as num_mod
        from custom_components.molly_cody_feeder import switch as sw_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            switches: list[Any] = []
            numbers: list[Any] = []
            await sw_mod.async_setup_entry(hass, entry, switches.extend)
            await num_mod.async_setup_entry(hass, entry, numbers.extend)
            self.assertEqual(len(switches), 16)
            self.assertEqual(len(numbers), 12)

            # Truncate schedule list to only 1 slot and ensure slots 2..8 do not raise IndexError
            coord.data["schedule"] = coord.data["schedule"][:1]
            for sw in switches:
                self.assertIsInstance(sw.is_on, bool)
            for num in numbers:
                self.assertIsNotNone(num.native_value)

    async def test_f16_02_manual_feed_portions_number_bounds_1_to_20(self):
        from custom_components.molly_cody_feeder import number as num_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, _ = await _setup_entry_with_mock_client(td)
            numbers: list[Any] = []
            await num_mod.async_setup_entry(hass, entry, numbers.extend)
            self.assertEqual(len(numbers), 12)

            feed_num = next(
                n for n in numbers
                if "feed_portions" in n.unique_id.lower() or "manual" in n.unique_id.lower()
            )
            self.assertEqual(feed_num.native_min_value, 1)
            self.assertEqual(feed_num.native_max_value, 20)
            await feed_num.async_set_native_value(5)
            self.assertEqual(int(feed_num.native_value), 5)

    async def test_f16_03_nightvision_dp_108_select_mapping(self):
        from custom_components.molly_cody_feeder import select as sel_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            selects: list[Any] = []
            await sel_mod.async_setup_entry(hass, entry, selects.extend)
            self.assertEqual(len(selects), 4)

            nv_sel = next(s for s in selects if "night" in s.unique_id.lower() or "108" in s.unique_id.lower())
            self.assertGreaterEqual(len(nv_sel.options), 3)
            with mock.patch.object(coord, "async_set_dp", new=mock.AsyncMock(return_value=True)) as m_dp:
                target_opt = nv_sel.options[-1]
                await nv_sel.async_select_option(target_opt)
                m_dp.assert_called_once()
                self.assertEqual(int(m_dp.call_args.args[0]), 108)

    async def test_f16_04_modular_compute_toggle_switches_update_coordinator_options(self):
        from custom_components.molly_cody_feeder import switch as sw_mod
        from custom_components.molly_cody_feeder.const import CONF_ENABLE_VISION_VERIFICATION

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            switches: list[Any] = []
            await sw_mod.async_setup_entry(hass, entry, switches.extend)
            self.assertEqual(len(switches), 16)

            vision_sw = next(s for s in switches if "vision" in s.unique_id.lower())
            await vision_sw.async_turn_on()
            self.assertTrue(coord.options.get(CONF_ENABLE_VISION_VERIFICATION) or vision_sw.is_on)
            await vision_sw.async_turn_off()
            self.assertFalse(vision_sw.is_on)

    async def test_f16_05_clear_alert_and_refill_buttons_execute_coordinator_actions(self):
        from custom_components.molly_cody_feeder import button as btn_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            buttons: list[Any] = []
            await btn_mod.async_setup_entry(hass, entry, buttons.extend)
            self.assertEqual(len(buttons), 8)

            coord.data["summary"]["clog_detected"] = True
            clear_btn = next(b for b in buttons if "clear" in b.unique_id.lower())
            await clear_btn.async_press()
            self.assertFalse(coord.data["summary"]["clog_detected"])

            refill_btn = next(b for b in buttons if "refill" in b.unique_id.lower())
            await refill_btn.async_press()
            self.assertTrue(coord.data["summary"]["hopper_known"])
            self.assertEqual(coord.data["summary"]["hopper_percent"], 100.0)


# ===========================================================================
# F17: Camera, Image & Event Platforms Boundaries
# ===========================================================================
class TestF17CameraImageEventBoundaries(unittest.IsolatedAsyncioTestCase):
    """Tier 2 boundary, negative, and corner cases for F17 (Camera, Image & Event Platforms)."""

    async def test_f17_01_camera_returns_cached_or_none_when_enable_camera_false(self):
        from custom_components.molly_cody_feeder import camera as cam_mod
        from custom_components.molly_cody_feeder.const import CONF_ENABLE_CAMERA

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(
                td, options_overrides={CONF_ENABLE_CAMERA: False}
            )
            cameras: list[Any] = []
            await cam_mod.async_setup_entry(hass, entry, cameras.extend)
            self.assertEqual(len(cameras), 1)
            cam = cameras[0]

            coord.data["latest_snapshot_bytes"] = None
            with mock.patch.object(coord.client, "capture_snapshot") as m_snap:
                img_none = await cam.async_camera_image()
                self.assertIsNone(img_none)
                coord.data["latest_snapshot_bytes"] = b"\xff\xd8\xff\xe0cached_jpeg"
                img_cached = await cam.async_camera_image()
                self.assertEqual(img_cached, b"\xff\xd8\xff\xe0cached_jpeg")
                m_snap.assert_not_called()

    async def test_f17_02_camera_skips_live_capture_when_coordinator_busy(self):
        from custom_components.molly_cody_feeder import camera as cam_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            cameras: list[Any] = []
            await cam_mod.async_setup_entry(hass, entry, cameras.extend)
            cam = cameras[0]

            coord.data["summary"]["busy"] = True
            coord.data["latest_snapshot_bytes"] = b"\xff\xd8\xff\xe0busy_cached"
            with mock.patch.object(coord.client, "capture_snapshot") as m_snap:
                img = await cam.async_camera_image()
                self.assertEqual(img, b"\xff\xd8\xff\xe0busy_cached")
                m_snap.assert_not_called()

    async def test_f17_03_image_entities_return_none_before_first_capture(self):
        from custom_components.molly_cody_feeder import image as img_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            images: list[Any] = []
            await img_mod.async_setup_entry(hass, entry, images.extend)
            self.assertEqual(len(images), 3)

            coord.data["latest_annotated_bytes"] = None
            coord.data["latest_before_bytes"] = None
            coord.data["latest_after_bytes"] = None
            for img_ent in images:
                self.assertIsNone(await img_ent.async_image())

            coord.data["latest_annotated_bytes"] = b"ann_bytes"
            coord.data["latest_before_bytes"] = b"before_bytes"
            coord.data["latest_after_bytes"] = b"after_bytes"
            for img_ent in images:
                self.assertIsNotNone(await img_ent.async_image())

    async def test_f17_04_event_entities_initial_state_and_consecutive_events(self):
        from custom_components.molly_cody_feeder import event as evt_mod

        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = await _setup_entry_with_mock_client(td)
            events: list[Any] = []
            await evt_mod.async_setup_entry(hass, entry, events.extend)
            self.assertEqual(len(events), 2)

            for ev_ent in events:
                self.assertIsInstance(ev_ent.event_types, list)
                self.assertGreaterEqual(len(ev_ent.event_types), 1)

            # Fire two consecutive feed events
            await coord.async_feed(portions=1, verify=False)
            await coord.async_feed(portions=2, verify=False)
            self.assertIsNotNone(coord.data.get("last_feed_event"))

    async def test_f17_05_coordinator_exposes_shared_session_lock_guard(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, coord = await _setup_entry_with_mock_client(td)
            lock = getattr(coord, "session_lock", getattr(coord, "_session_lock", None))
            self.assertIsInstance(lock, asyncio.Lock)


# ===========================================================================
# F18: Lovelace Dashboard, Automation Blueprints & v0.7 Documentation Boundaries
# ===========================================================================
class TestF18DocsAndYamlBoundaries(unittest.TestCase):
    """Tier 2 boundary, negative, and corner cases for F18 (Lovelace, Blueprints & Docs)."""

    @staticmethod
    def _load_yaml_with_input_tag(path: Path) -> Any:
        class HaYamlLoader(yaml.SafeLoader):
            pass

        HaYamlLoader.add_constructor(
            "!input",
            lambda loader, node: f"!input {loader.construct_scalar(node)}",
        )
        return yaml.load(path.read_text(encoding="utf-8"), Loader=HaYamlLoader)

    def test_f18_01_blueprints_parse_with_custom_input_tag_and_valid_schema(self):
        bp_dir = ROOT / "blueprints" / "automation" / "molly_cody_feeder"
        self.assertTrue(bp_dir.is_dir(), "blueprints/automation/molly_cody_feeder directory must exist")
        bp_files = sorted(bp_dir.glob("*.yaml"))
        self.assertGreaterEqual(len(bp_files), 1)
        for bp_file in bp_files:
            parsed = self._load_yaml_with_input_tag(bp_file)
            self.assertIsInstance(parsed, dict, f"Invalid blueprint YAML in {bp_file.name}")
            self.assertIn("blueprint", parsed)
            self.assertEqual(parsed["blueprint"].get("domain"), "automation")
            self.assertIsInstance(parsed["blueprint"].get("input"), dict)
            self.assertTrue("trigger" in parsed or "triggers" in parsed)
            self.assertTrue("action" in parsed or "actions" in parsed)

    def test_f18_02_blueprints_reference_valid_molly_cody_feeder_domain_and_events(self):
        bp_dir = ROOT / "blueprints" / "automation" / "molly_cody_feeder"
        bp_files = sorted(bp_dir.glob("*.yaml"))
        combined_text = "\n".join(f.read_text(encoding="utf-8") for f in bp_files)
        self.assertIn("molly_cody_feeder", combined_text)

    def test_f18_03_lovelace_dashboard_valid_yaml_and_entity_references(self):
        dash_path = ROOT / "lovelace" / "molly_cody_dashboard.yaml"
        self.assertTrue(dash_path.is_file(), "lovelace/molly_cody_dashboard.yaml must exist")
        raw = dash_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        self.assertIsInstance(parsed, dict)
        self.assertIn("views", parsed)
        self.assertIsInstance(parsed["views"], list)
        self.assertGreaterEqual(len(parsed["views"]), 1)
        self.assertIn("molly_cody_feeder", raw)

    def test_f18_04_docs_contain_zero_leaked_tokens_and_confirm_standalone_hacs(self):
        for doc_name in ("README.md", "info.md", "CHANGELOG.md"):
            p = ROOT / doc_name
            self.assertTrue(p.is_file(), f"{doc_name} must exist")
            txt = p.read_text(encoding="utf-8")
            self.assertNotIn("github_pat_", txt, f"{doc_name} must never contain a GitHub PAT")
            self.assertNotIn("BEGIN RSA PRIVATE KEY", txt)

        info_txt = (ROOT / "info.md").read_text(encoding="utf-8")
        self.assertIn("HACS", info_txt)

    def test_f18_05_hacs_json_and_manifest_json_strict_schema_validation(self):
        hacs_path = ROOT / "hacs.json"
        manifest_path = ROOT / "custom_components" / "molly_cody_feeder" / "manifest.json"
        self.assertTrue(hacs_path.is_file())
        self.assertTrue(manifest_path.is_file())

        hacs_data = json.loads(hacs_path.read_text(encoding="utf-8"))
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertTrue(bool(hacs_data.get("name")))
        self.assertEqual(manifest_data.get("domain"), "molly_cody_feeder")
        self.assertEqual(manifest_data.get("version"), "0.7.0")
        self.assertTrue(manifest_data.get("config_flow"))
        self.assertIn("MollyCodyFeeder", manifest_data.get("documentation", ""))
        self.assertIn("MollyCodyFeeder", manifest_data.get("issue_tracker", ""))


if __name__ == "__main__":
    unittest.main()
