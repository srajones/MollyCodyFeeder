"""Tier 3 E2E Tests: Cross-Feature Combinations (`test_tier3_combinations.py`).

Exercises pairwise and multi-feature interactions across F1–F18 of the standalone
Home Assistant HACS integration (`custom_components/molly_cody_feeder/` v0.7.0)
strictly against the public interface contracts in `PROJECT.md` and `TEST_INFRA.md`.

All tests run hermetically offline using `tempfile.TemporaryDirectory()` and
mocked external network transports (zero live cloud/LAN/UDP sockets).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import numpy as np
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Real 5-slot DP 207 schedule string (4 enabled daily slots + 1 disabled one-off slot)
LIVE_SCHEDULE = (
    "7f0700201000000"
    "7f1100201000000"
    "7f1600201000000"
    "7f1900171000000"
    "000908100000000"
)

ALL_DISABLED_SCHEDULE = (
    "7f0700200000000"
    "7f1100200000000"
    "7f1600200000000"
    "7f1900170000000"
    "000908100000000"
)


# ---------------------------------------------------------------------------
# Hermetic Synthetic Media Helpers (8000 Hz WAV + 720x1280 Upright JPEG)
# ---------------------------------------------------------------------------


def write_synthetic_dispense_wav(
    wav_path: str | Path,
    *,
    motor_sec: float = 3.0,
    with_tings: bool = True,
    ting_spacing_sec: float = 0.10,
    sample_rate: int = 8000,
) -> Path:
    """Writes a physics-accurate 8000 Hz mono 16-bit WAV file.

    - 1.0s pre-silence (low floor)
    - `motor_sec` auger motor hum (180 Hz + 320 Hz sustained block >= 1.2s)
    - Optional STFT-aligned kibble 'ting' bursts (1100 Hz + 3100 Hz, spectral ratio > 1.6)
    - 1.0s post-silence
    """
    path = Path(wav_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    total_dur = 2.0 + float(motor_sec)
    t = np.arange(int(sample_rate * total_dur)) / float(sample_rate)
    samples = 0.001 * np.sin(2.0 * np.pi * 90.0 * t)

    m_start = 1.0
    m_end = 1.0 + float(motor_sec)
    m_mask = (t >= m_start) & (t < m_end)
    if motor_sec > 0:
        samples[m_mask] += (
            0.05 * np.sin(2.0 * np.pi * 180.0 * t[m_mask])
            + 0.03 * np.sin(2.0 * np.pi * 320.0 * t[m_mask])
        )

    if with_tings and motor_sec > 0.3:
        for onset in np.arange(m_start + 0.10, m_end - 0.15, ting_spacing_sec):
            i0 = int(round(onset * sample_rate))
            i1 = min(len(samples), i0 + int(0.015 * sample_rate))
            tt = np.arange(i1 - i0) / float(sample_rate)
            env = np.hanning(len(tt))
            samples[i0:i1] += env * (
                0.45 * np.sin(2.0 * np.pi * 1100.0 * tt)
                + 0.45 * np.sin(2.0 * np.pi * 3100.0 * tt)
            )

    pcm16 = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return path


def write_synthetic_bowl_jpeg(
    jpg_path: str | Path,
    *,
    is_ir: bool = False,
    kibble_fill: float = 0.0,
    width: int = 720,
    height: int = 1280,
) -> Path:
    """Writes a synthetic 720x1280 upright bowl JPEG (daylight color or IR grayscale).

    - Daylight (`is_ir=False`): high HSV saturation background (`sat > 50`) so `_detect_infrared_mode`
      is `False` and `_lit_frame` is `True`. `DAY_PIT` (`0.32..0.75` x, `0.88..0.985` y) is bright
      stainless steel (value ~170) with a fraction (`kibble_fill`) of very dark kibble pixels (`< 4`).
    - IR night-vision (`is_ir=True`): zero color saturation (`R == G == B`, `HSV sat == 0 < 14.0`),
      bright IR reflection in `IR_BAND` (`0.22..0.88` x, `0.88..0.995` y) occluded by dark kibble
      silhouettes when `kibble_fill > 0`.
    """
    path = Path(jpg_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_ir:
        arr = np.full((height, width, 3), 135, dtype=np.uint8)
        # Bright hotspot in bottom bowl band (0.93..0.995 y, 0.30..0.80 x)
        hy1, hy2 = int(0.88 * height), int(0.995 * height)
        hx1, hx2 = int(0.22 * width), int(0.88 * width)
        arr[hy1:hy2, hx1:hx2] = 220
        if kibble_fill > 0:
            fill_h = max(1, int((hy2 - hy1) * min(1.0, kibble_fill)))
            arr[hy1 : hy1 + fill_h, hx1:hx2] = 25
    else:
        # Warm color scene (RGB = [195, 145, 90] -> high HSV saturation >= 60)
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        arr[:, :, 0] = 195
        arr[:, :, 1] = 145
        arr[:, :, 2] = 90
        # Bright stainless steel DAY_PIT (0.32..0.75 x, 0.88..0.985 y)
        py1, py2 = int(0.88 * height), int(0.985 * height)
        px1, px2 = int(0.32 * width), int(0.75 * width)
        arr[py1:py2, px1:px2] = [180, 175, 165]
        if kibble_fill > 0:
            # _compute_roi_kibble_fill subtracts a 0.130 rim baseline from dark pixels (< 6.5 gray)
            target_dark = min(0.95, 0.16 + float(kibble_fill))
            pit_h = py2 - py1
            dark_rows = max(1, int(pit_h * target_dark))
            arr[py1 : py1 + dark_rows, px1:px2] = [1, 1, 1]
            # Add checkerboard texture in bowl floor (0.925..0.995 y, 0.14..0.88 x)
            ty1, ty2 = int(0.925 * height), int(0.995 * height)
            tx1, tx2 = int(0.14 * width), int(0.88 * width)
            grid = (np.indices((ty2 - ty1, tx2 - tx1)).sum(axis=0) % 4 == 0)
            sub = arr[ty1:ty2, tx1:tx2]
            sub[grid] = [2, 2, 2]

    Image.fromarray(arr, mode="RGB").save(str(path), format="JPEG", quality=98)
    return path


# ---------------------------------------------------------------------------
# Resilient Home Assistant & Integration Harness Helpers
# ---------------------------------------------------------------------------


def make_hass_and_entry(
    tmp_dir: str | Path,
    *,
    entry_id: str = "entry_t3_001",
    data: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
):
    """Creates a standalone `HomeAssistant` and `ConfigEntry` backed by `tmp_dir`."""
    from custom_components.molly_cody_feeder import _ha_compat as hc
    from custom_components.molly_cody_feeder import const

    default_data = {
        const.CONF_EMAIL: "molly.owner@example.com",
        const.CONF_PASSWORD: "molly_secret_password",
        const.CONF_COUNTRY_CODE: "1",
        const.CONF_DEVICE_ID: "dev_molly_6l_001",
        const.CONF_DEVICE_NAME: "Molly & Cody Feeder",
        const.CONF_LOCAL_IP: "192.168.1.155",
        const.CONF_LOCAL_KEY: "0123456789abcdef",
        const.CONF_CONNECTION_MODE: "auto",
    }
    if data:
        default_data.update(data)

    default_options = {
        const.CONF_CONNECTION_MODE: default_data.get(const.CONF_CONNECTION_MODE, "auto"),
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
    if options:
        default_options.update(options)

    # Instantiate HomeAssistant from _ha_compat
    try:
        hass = hc.HomeAssistant(str(tmp_dir))
    except TypeError:
        hass = hc.HomeAssistant()

    # Ensure hass.config.path resolves under tmp_dir
    if not hasattr(hass, "config") or hass.config is None:
        hass.config = mock.MagicMock()
    hass.config.config_dir = str(tmp_dir)
    hass.config.path = lambda *parts: str(Path(tmp_dir).joinpath(*parts))

    if not hasattr(hass, "data") or not isinstance(hass.data, dict):
        hass.data = {}

    # Track fired events on hass.bus while preserving underlying bus behavior
    fired_events: List[Tuple[str, Dict[str, Any]]] = []
    orig_async_fire = getattr(hass.bus, "async_fire", None)
    orig_fire = getattr(hass.bus, "fire", None)

    def _record_fire(event_type: str, event_data: Optional[Dict[str, Any]] = None, **kw):
        payload = dict(event_data or {})
        fired_events.append((event_type, payload))
        if callable(orig_async_fire):
            res = orig_async_fire(event_type, payload, **kw)
            if inspect.isawaitable(res):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(res)
                except RuntimeError:
                    pass
            return res
        if callable(orig_fire):
            return orig_fire(event_type, payload, **kw)
        return None

    hass.bus.async_fire = _record_fire
    hass.bus.fire = _record_fire
    hass._fired_events = fired_events

    # Instantiate ConfigEntry from _ha_compat
    try:
        entry = hc.ConfigEntry(
            entry_id=entry_id,
            domain=const.DOMAIN,
            title=default_data.get(const.CONF_DEVICE_NAME, "Molly & Cody Feeder"),
            data=default_data,
            options=default_options,
        )
    except TypeError:
        entry = hc.ConfigEntry()
        entry.entry_id = entry_id
        entry.domain = const.DOMAIN
        entry.title = default_data.get(const.CONF_DEVICE_NAME, "Molly & Cody Feeder")
        entry.data = default_data
        entry.options = default_options

    # Register entry on hass.config_entries if supported
    if hasattr(hass, "config_entries"):
        entries_store = getattr(hass.config_entries, "_entries", None)
        if isinstance(entries_store, dict):
            entries_store[entry.entry_id] = entry
        elif isinstance(entries_store, list):
            entries_store.append(entry)

    return hass, entry


def get_coordinator(hass, entry):
    """Retrieves the `MollyCodyDataUpdateCoordinator` from `hass.data[DOMAIN][entry.entry_id]`."""
    from custom_components.molly_cody_feeder import const

    domain_data = hass.data.get(const.DOMAIN, {})
    slot = domain_data.get(entry.entry_id)
    if isinstance(slot, dict) and "coordinator" in slot:
        return slot["coordinator"]
    return slot


async def setup_platform_entities(hass, entry, platform_name: str) -> List[Any]:
    """Sets up a specific entity platform (`sensor`, `binary_sensor`, `switch`, etc.) and returns entities."""
    mod = __import__(
        f"custom_components.molly_cody_feeder.{platform_name}",
        fromlist=["async_setup_entry"],
    )
    entities: List[Any] = []

    def _add_entities(new_entities, update_before_add: bool = False):
        entities.extend(list(new_entities))

    res = mod.async_setup_entry(hass, entry, _add_entities)
    if inspect.isawaitable(res):
        await res
    for ent in entities:
        if getattr(ent, "hass", None) is None:
            ent.hass = hass
    return entities


def find_entity(entities: List[Any], *candidates: str) -> Any:
    """Locates an entity by description key, translation key, unique_id suffix, or name."""
    norm_cands = [c.lower().strip() for c in candidates]
    for cand in norm_cands:
        for ent in entities:
            desc = getattr(ent, "entity_description", None)
            keys = [
                getattr(desc, "key", None),
                getattr(ent, "key", None),
                getattr(ent, "_key", None),
                getattr(ent, "translation_key", None),
                getattr(ent, "_attr_translation_key", None),
            ]
            for k in keys:
                if k and str(k).lower() == cand:
                    return ent
            uid = getattr(ent, "unique_id", None) or getattr(ent, "_attr_unique_id", None)
            if uid and (str(uid).lower().endswith(f"_{cand}") or str(uid).lower() == cand):
                return ent
    for cand in norm_cands:
        for ent in entities:
            desc = getattr(ent, "entity_description", None)
            name = (
                getattr(ent, "name", None)
                or getattr(ent, "_attr_name", None)
                or getattr(desc, "name", None)
                or ""
            )
            uid = getattr(ent, "unique_id", None) or getattr(ent, "_attr_unique_id", None) or ""
            if cand in str(name).lower() or cand in str(uid).lower():
                return ent
    avail = [
        getattr(getattr(e, "entity_description", None), "key", None)
        or getattr(e, "unique_id", None)
        or getattr(e, "name", None)
        for e in entities
    ]
    raise AssertionError(f"Could not find entity matching {candidates!r} among {avail!r}")


def entity_sensor_value(entity: Any) -> Any:
    """Reads `native_value` (or `state` fallback) from a `SensorEntity`."""
    val = getattr(entity, "native_value", None)
    if val is not None:
        return val
    return getattr(entity, "state", None)


async def call_ha_service(hass, domain: str, service: str, data: Optional[Dict[str, Any]] = None):
    """Invokes a registered service on `hass.services` (compatible with both HA and `_ha_compat`)."""
    from custom_components.molly_cody_feeder import _ha_compat as hc

    payload = dict(data or {})
    if hasattr(hass.services, "async_call"):
        try:
            res = hass.services.async_call(domain, service, payload, blocking=True)
        except TypeError:
            res = hass.services.async_call(domain, service, payload)
        if inspect.isawaitable(res):
            return await res
        return res

    # Fallback if services registry stores handlers in a dict
    handlers = getattr(hass.services, "_services", {})
    handler = handlers.get(domain, {}).get(service) or handlers.get((domain, service))
    if handler is None:
        raise AssertionError(f"Service {domain}.{service} is not registered")
    call_obj = hc.ServiceCall(domain, service, payload)
    res = handler(call_obj)
    if inspect.isawaitable(res):
        return await res
    return res


def record_db_feed(db, **kwargs) -> int:
    """Records a feed event in `KibbleDatabase` supporting both `record_feed_event` and `log_feed_event`."""
    defaults = {
        "event_type": "manual_dispense_verify",
        "mode": "auto",
        "transport": "cloud",
        "portions": 1,
        "success": True,
        "clog_detected": False,
        "audio_state": "dispensed",
        "motor_detected": True,
        "kibble_peaks": 28,
        "motor_db": 26.0,
        "impact_db": 15.0,
        "before_fill_pct": 2.0,
        "after_fill_pct": 14.0,
        "fill_delta_pct": 12.0,
        "bowl_empty": False,
        "food_increased": True,
        "dog_in_bowl": False,
        "infrared_mode": False,
        "food_weight_g": 10,
        "feed_abnormal": 1,
        "outcome": "dispensed",
        "motor_duration_sec": 3.0,
        "kibble_rate": 9.3,
    }
    defaults.update(kwargs)
    if hasattr(db, "record_feed_event"):
        try:
            return db.record_feed_event(defaults)
        except TypeError:
            return db.record_feed_event(**defaults)
    return db.log_feed_event(**defaults)


# ===========================================================================
# Tier 3 Cross-Feature Combination Test Suite (>= 28 Tests Across F1–F18)
# ===========================================================================


class TestTier3CrossFeatureCombinations(unittest.IsolatedAsyncioTestCase):
    """Tier 3: Pairwise and multi-feature combinations across F1–F18."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="molly_t3_")
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mock_client_network(
        self,
        client,
        *,
        dps: Optional[Dict[str, Any]] = None,
        lan_ok: bool = True,
        cloud_ok: bool = True,
    ):
        """Configures a `MollyFeederClient` instance so all network I/O runs hermetically in-memory."""
        initial_dps = {
            "101": True,
            "103": False,
            "104": True,
            "106": "1",
            "108": "0",
            "134": True,
            "201": 1,
            "202": 10,
            "207": LIVE_SCHEDULE,
            "255": 1,
        }
        if dps:
            initial_dps.update({str(k): v for k, v in dps.items()})
        client.cached_dps = dict(initial_dps)
        client.device_id = client.device_id or "dev_molly_6l_001"
        client.device_name = client.device_name or "Molly & Cody Feeder"
        client.local_ip = client.local_ip or "192.168.1.155"
        client.local_key = client.local_key or "0123456789abcdef"
        client.sid = client.sid or "sid_live_001"
        client.uid = client.uid or "uid_live_001"
        client.ecode = client.ecode or "ecode_live_001"
        client.is_online = True

        def _get_dps_lan():
            if not lan_ok or not client.local_ip or not client.local_key:
                return None
            return dict(client.cached_dps)

        def _get_dps_cloud():
            if not cloud_ok:
                return None
            return dict(client.cached_dps)

        def _set_dps_lan(new_dps: Dict[str, Any]):
            if not lan_ok or not client.local_ip or not client.local_key:
                return False
            client.cached_dps.update({str(k): v for k, v in new_dps.items()})
            return True

        def _set_dps_cloud(new_dps: Dict[str, Any], wait_ack_sec: float = 3.5):
            if not cloud_ok:
                return False
            client.cached_dps.update({str(k): v for k, v in new_dps.items()})
            return True

        client.get_dps_lan = mock.MagicMock(side_effect=_get_dps_lan)
        client.get_dps_cloud = mock.MagicMock(side_effect=_get_dps_cloud)
        client.set_dps_lan = mock.MagicMock(side_effect=_set_dps_lan)
        client.set_dps_cloud = mock.MagicMock(side_effect=_set_dps_cloud)
        client.refresh_cloud_devices = mock.MagicMock(return_value=cloud_ok)
        return client

    async def _setup_integration(
        self,
        *,
        data: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        dps: Optional[Dict[str, Any]] = None,
        lan_ok: bool = True,
        cloud_ok: bool = True,
    ):
        """Sets up the integration via `async_setup_entry` with hermetic client & storage."""
        import custom_components.molly_cody_feeder as init_mod
        from custom_components.molly_cody_feeder.core import MollyFeederClient

        hass, entry = make_hass_and_entry(self.tmp_path, data=data, options=options)

        orig_init = MollyFeederClient.__init__

        def _patched_init(c_self, *args, **kwargs):
            orig_init(c_self, *args, **kwargs)
            self._mock_client_network(c_self, dps=dps, lan_ok=lan_ok, cloud_ok=cloud_ok)

        with mock.patch.object(MollyFeederClient, "__init__", _patched_init):
            ok = await init_mod.async_setup_entry(hass, entry)
            self.assertTrue(ok, "async_setup_entry must succeed")

        coordinator = get_coordinator(hass, entry)
        self.assertIsNotNone(coordinator, "Coordinator must be stored in hass.data")
        return hass, entry, coordinator

    # -----------------------------------------------------------------------
    # T3_01: Cloud Login + ConfigFlow Entry Creation (F1 + F12)
    # -----------------------------------------------------------------------
    async def test_t3_01_cloud_login_and_config_flow_entry_creation(self):
        """F1 + F12: Cloud login & device discovery feed directly into ConfigFlow.async_step_user."""
        from custom_components.molly_cody_feeder import config_flow as cf_mod
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.core import MollyFeederClient

        hass, _ = make_hass_and_entry(self.tmp_path)

        def _fake_raw_api_call(c_self, action, version, post_data_obj, sid="", gid=0):
            if action == "smartlife.m.user.username.token.get":
                return {"success": True, "result": {"token": "tok_abc123", "publicKey": "", "exponent": ""}}
            if action == "smartlife.m.user.email.password.login":
                return {
                    "success": True,
                    "result": {
                        "sid": "sid_cloud_999",
                        "uid": "uid_cloud_888",
                        "ecode": "ecode_cloud_777",
                        "partnerIdentity": "p2155085",
                    },
                }
            if action == "smartlife.m.location.list":
                return {"success": True, "result": [{"groupId": 424242}]}
            if action == "m.life.my.group.device.list":
                return {
                    "success": True,
                    "result": [
                        {
                            "devId": "dev_discovered_6l",
                            "localKey": "key_discovered_16",
                            "name": "Hallway Molly Feeder",
                            "category": "sp",
                            "cloudOnline": True,
                            "dps": {"201": 1, "202": 10, "207": LIVE_SCHEDULE, "255": 1},
                        }
                    ],
                }
            return {"success": True, "result": True}

        flow_cls = getattr(cf_mod, "MollyCodyConfigFlow", None) or getattr(cf_mod, "ConfigFlow")
        flow = flow_cls()
        flow.hass = hass

        with mock.patch.object(MollyFeederClient, "_raw_api_call", _fake_raw_api_call):
            result = await flow.async_step_user(
                {
                    const.CONF_EMAIL: "molly.owner@example.com",
                    const.CONF_PASSWORD: "correct_password",
                    const.CONF_COUNTRY_CODE: "1",
                    const.CONF_CONNECTION_MODE: "auto",
                }
            )

        self.assertEqual(result.get("type"), "create_entry")
        entry_data = result.get("data", {})
        self.assertEqual(entry_data.get(const.CONF_DEVICE_ID), "dev_discovered_6l")
        self.assertEqual(entry_data.get(const.CONF_LOCAL_KEY), "key_discovered_16")
        self.assertEqual(entry_data.get(const.CONF_DEVICE_NAME), "Hallway Molly Feeder")
        self.assertEqual(entry_data.get(const.CONF_EMAIL), "molly.owner@example.com")

    # -----------------------------------------------------------------------
    # T3_02: Session Persistence (0o600) + Coordinator Reload (F1 + F11 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_02_session_persistence_and_coordinator_reload(self):
        """F1 + F11 + F13: Atomic 0o600 session saved in .storage is reloaded by Coordinator without re-login."""
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.coordinator import MollyCodyDataUpdateCoordinator
        from custom_components.molly_cody_feeder.core import MollyFeederClient, RuntimeConfig

        hass, entry = make_hass_and_entry(self.tmp_path, entry_id="entry_persist_02")
        rt_cfg = RuntimeConfig.from_entry(hass, entry)

        # Save session via first client instance
        client1 = MollyFeederClient(
            email="molly.owner@example.com",
            password="pw",
            device_id="dev_persisted_02",
            local_key="local_key_persisted",
            local_ip="192.168.1.200",
            session_file=str(rt_cfg.session_file),
        )
        client1.sid = "sid_persisted_xyz"
        client1.uid = "uid_persisted_xyz"
        client1.ecode = "ecode_persisted_xyz"
        client1.device_name = "Persisted Feeder"
        client1.cached_dps = {"201": 2, "202": 20, "207": LIVE_SCHEDULE, "255": 1}
        client1._save_session()

        self.assertTrue(rt_cfg.session_file.exists())
        file_mode = rt_cfg.session_file.stat().st_mode & 0o777
        self.assertEqual(file_mode, 0o600, "Session file must have strict 0o600 permissions")

        # Now initialize coordinator and verify it loads the persisted session without forced login
        with mock.patch.object(MollyFeederClient, "refresh_cloud_devices", return_value=True), \
             mock.patch.object(MollyFeederClient, "get_dps_lan", return_value=client1.cached_dps):
            coord = MollyCodyDataUpdateCoordinator(hass, entry)
            await coord.async_refresh()

        client2 = getattr(coord, "client", None) or getattr(coord, "molly", None)
        self.assertIsNotNone(client2)
        self.assertEqual(client2.sid, "sid_persisted_xyz")
        self.assertEqual(client2.uid, "uid_persisted_xyz")
        self.assertEqual(client2.ecode, "ecode_persisted_xyz")
        self.assertEqual(coord.data.get("device_id"), "dev_persisted_02")
        self.assertIn(str(const.DOMAIN), str(rt_cfg.session_file))

    # -----------------------------------------------------------------------
    # T3_03: Transport Mode Select Entity + Coordinator + Client (F2 + F13 + F16)
    # -----------------------------------------------------------------------
    async def test_t3_03_transport_select_entity_updates_coordinator_and_client_mode(self):
        """F2 + F13 + F16: Selecting 'lan' or 'cloud' on Mode SelectEntity updates coordinator, client, and sensor."""
        hass, entry, coordinator = await self._setup_integration()
        selects = await setup_platform_entities(hass, entry, "select")
        sensors = await setup_platform_entities(hass, entry, "sensor")

        mode_select = find_entity(selects, "connection_mode", "mode", "Connection Mode")
        mode_sensor = find_entity(sensors, "configured_mode", "connection_mode", "mode")

        self.assertIn("auto", mode_select.options)
        self.assertIn("cloud", mode_select.options)
        self.assertIn("lan", mode_select.options)

        await mode_select.async_select_option("lan")
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        self.assertEqual(client.mode, "lan")
        self.assertEqual(coordinator.data["configured_mode"], "lan")
        self.assertEqual(mode_select.current_option, "lan")
        self.assertEqual(entity_sensor_value(mode_sensor), "lan")

        await mode_select.async_select_option("cloud")
        self.assertEqual(client.mode, "cloud")
        self.assertEqual(coordinator.data["configured_mode"], "cloud")
        self.assertEqual(mode_select.current_option, "cloud")
        self.assertEqual(entity_sensor_value(mode_sensor), "cloud")

    # -----------------------------------------------------------------------
    # T3_04: Auto Mode LAN Read Fallback to Cloud + Transport Sensor (F2 + F13 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_04_auto_mode_lan_read_fallback_to_cloud_and_sensor_transport(self):
        """F2 + F13 + F15: In 'auto' mode, LAN timeout falls back to Cloud and updates Active Transport sensor."""
        hass, entry, coordinator = await self._setup_integration(lan_ok=True, cloud_ok=True)
        sensors = await setup_platform_entities(hass, entry, "sensor")
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")

        transport_sensor = find_entity(sensors, "active_transport", "transport", "Active Transport")
        online_bs = find_entity(binary_sensors, "online", "connectivity")

        # Initial poll in 'auto' mode uses LAN first
        await coordinator.async_refresh()
        self.assertEqual(coordinator.data["active_transport"], "lan")
        self.assertEqual(entity_sensor_value(transport_sensor), "lan")
        self.assertTrue(online_bs.is_on)

        # Simulate LAN TCP 6668 timeout while Cloud remains reachable
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.get_dps_lan = mock.MagicMock(return_value=None)
        client.get_dps_cloud = mock.MagicMock(return_value=dict(client.cached_dps))

        await coordinator.async_refresh()
        self.assertEqual(coordinator.data["active_transport"], "cloud")
        self.assertEqual(entity_sensor_value(transport_sensor), "cloud")
        self.assertTrue(online_bs.is_on)

    # -----------------------------------------------------------------------
    # T3_05: Manual Feed Service + DP 201 + Hopper Decrement + Event (F3 + F10 + F13 + F14)
    # -----------------------------------------------------------------------
    async def test_t3_05_manual_feed_service_writes_dp201_decrements_hopper_and_fires_event(self):
        """F3 + F10 + F13 + F14: Service `feed(portions=3, verify=False)` writes DP 201, logs DB, decrements hopper, fires EVENT_FEED."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration()
        await coordinator.async_refill_hopper(capacity_portions=200, note="Initial 200p fill")
        self.assertEqual(coordinator.data["summary"]["hopper_portions_left"], 200)

        await call_ha_service(
            hass,
            const.DOMAIN,
            const.SERVICE_FEED,
            {"portions": 3, "verify": False},
        )

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        self.assertEqual(client.cached_dps.get("201"), 3)
        self.assertEqual(coordinator.data["summary"]["hopper_portions_left"], 197)
        self.assertEqual(coordinator.data["summary"]["hopper_portions_used"], 3)

        feed_events = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_FEED]
        self.assertGreaterEqual(len(feed_events), 1)
        self.assertEqual(feed_events[-1].get("portions"), 3)

    # -----------------------------------------------------------------------
    # T3_06: DP 255 Hardware Fault + Verifier Failure + Binary Sensor (F3 + F9 + F13 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_06_dp255_hardware_fault_triggers_verifier_failure_and_binary_sensor(self):
        """F3 + F9 + F13 + F15: DP 255 == 1 is normal, whereas DP 255 == 2 (> 1) fails verification and activates fault BinarySensor."""
        from custom_components.molly_cody_feeder.core import AudioAnalysisResult, decide_passed

        hass, entry, coordinator = await self._setup_integration(dps={"255": 1})
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")
        fault_bs = find_entity(binary_sensors, "hardware_fault", "feed_abnormal", "fault")

        good_audio = AudioAnalysisResult(
            motor_detected=True,
            kibble_impact_peaks=30,
            kibble_dispensed_audio=True,
            motor_energy_db=28.0,
            impact_energy_db=16.0,
            diagnosis="Motor churned and kibble heard",
            motor_duration_sec=3.0,
            kibble_rate_per_sec=10.0,
            ting_count=25,
            ting_rate_per_sec=7.0,
        )

        # DP 255 == 1 is normal on every Molly & Cody dispense
        self.assertTrue(decide_passed(good_audio, None, feed_abnormal=1, expect_dispense=True))
        await coordinator.async_refresh()
        self.assertEqual(coordinator.data["feed_abnormal"], 1)
        self.assertFalse(fault_bs.is_on)

        # DP 255 == 2 (> 1) indicates a real hardware fault
        self.assertFalse(decide_passed(good_audio, None, feed_abnormal=2, expect_dispense=True))
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.cached_dps["255"] = 2
        await coordinator.async_refresh()
        self.assertEqual(coordinator.data["feed_abnormal"], 2)
        self.assertTrue(fault_bs.is_on)
        self.assertEqual(coordinator.data["summary"]["status"], "problem")

    # -----------------------------------------------------------------------
    # T3_07: Schedule Slot Switch & Number Entities + DP 207 Codec (F4 + F13 + F16)
    # -----------------------------------------------------------------------
    async def test_t3_07_schedule_slot_switch_and_number_entities_encode_dp207(self):
        """F4 + F13 + F16: Toggling Slot 2 Switch & setting Slot 2 Portions Number re-encodes DP 207 and updates Schedule Sensor."""
        from custom_components.molly_cody_feeder.core import decode_molly_schedule

        hass, entry, coordinator = await self._setup_integration(dps={"207": LIVE_SCHEDULE})
        switches = await setup_platform_entities(hass, entry, "switch")
        numbers = await setup_platform_entities(hass, entry, "number")
        sensors = await setup_platform_entities(hass, entry, "sensor")

        slot2_switch = find_entity(switches, "slot_2_enabled", "schedule_slot_2", "slot_2")
        slot2_number = find_entity(numbers, "slot_2_portions", "schedule_slot_2_portions", "slot_2")
        sched_sensor = find_entity(sensors, "schedule", "meal_schedule", "next_meal_time")

        self.assertTrue(slot2_switch.is_on)
        self.assertEqual(int(slot2_number.native_value), 20)

        await slot2_number.async_set_native_value(6)
        await slot2_switch.async_turn_off()

        raw_after = coordinator.data["schedule_raw"]
        decoded = decode_molly_schedule(raw_after)
        self.assertEqual(len(decoded), 5)
        self.assertEqual(decoded[1].slot, 2)
        self.assertEqual(decoded[1].portions, 6)
        self.assertFalse(decoded[1].enabled)
        # Slots 1, 3, 4 stay enabled with original portions
        self.assertEqual([decoded[0].portions, decoded[2].portions, decoded[3].portions], [20, 20, 17])
        self.assertTrue(all(decoded[i].enabled for i in (0, 2, 3)))
        self.assertFalse(slot2_switch.is_on)
        self.assertEqual(int(slot2_number.native_value), 6)
        self.assertIsNotNone(entity_sensor_value(sched_sensor))

    # -----------------------------------------------------------------------
    # T3_08: Schedule Change / All-Slots-Disabled Detection + Alert Event (F4 + F13 + F17)
    # -----------------------------------------------------------------------
    async def test_t3_08_schedule_change_detection_fires_alert_event(self):
        """F4 + F13 + F17: Disabling all schedule slots in DP 207 triggers schedule_empty alert and updates alert EventEntity."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(dps={"207": LIVE_SCHEDULE})
        events = await setup_platform_entities(hass, entry, "event")
        alert_event_ent = find_entity(events, "alert_event", "alert")

        await coordinator.async_sync_schedules()

        # Now simulate all schedule slots disabled on the feeder
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.cached_dps["207"] = ALL_DISABLED_SCHEDULE
        await coordinator.async_sync_schedules()
        await coordinator.async_refresh()

        alert_bus_events = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_ALERT]
        self.assertGreaterEqual(len(alert_bus_events), 1)
        last_alert = coordinator.data.get("last_alert_event") or alert_bus_events[-1]
        self.assertIsNotNone(last_alert)
        self.assertIsNotNone(alert_event_ent)

    # -----------------------------------------------------------------------
    # T3_09: Audio-Only Verification Mode Skips Video Decode & Passes Ting (F5 + F7 + F9 + F12)
    # -----------------------------------------------------------------------
    async def test_t3_09_audio_only_verification_mode_skips_video_decode_and_passes_ting(self):
        """F5 + F7 + F9 + F12: With enable_verification=True & enable_vision_verification=False, audio tings pass without vision."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_CAMERA: True,
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
            }
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _fake_capture_av_clip(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            out_wav = kwargs.get("output_wav")
            if out_wav:
                write_synthetic_dispense_wav(out_wav, motor_sec=3.0, with_tings=True)
            return {
                "video_ok": False,
                "audio_ok": True,
                "before_ok": False,
                "after_ok": False,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_fake_capture_av_clip)
        analyzer = getattr(coordinator, "analyzer", None)
        if analyzer is not None:
            analyzer.analyze_bowl_visual = mock.MagicMock(
                side_effect=AssertionError("analyze_bowl_visual must not be called when vision verification is disabled")
            )

        res = await coordinator.async_verify_feed(portions=2, trigger="manual_dispense_verify")
        self.assertEqual(res.get("outcome"), "dispensed")
        self.assertTrue(res.get("food_dispensed", res.get("passed", True)))
        self.assertFalse(res.get("clog_detected", False))
        self.assertEqual(coordinator.data["summary"]["last_meal_result"], "fed")
        self.assertTrue(coordinator.data["summary"]["last_meal_ok"])

    # -----------------------------------------------------------------------
    # T3_10: Camera Entity + Feed Verification Shared Session Lock (F5 + F13 + F17)
    # -----------------------------------------------------------------------
    async def test_t3_10_camera_snapshot_and_feed_verification_share_session_lock(self):
        """F5 + F13 + F17: Camera snapshot respects coordinator busy/session lock so two imm sessions never overlap."""
        hass, entry, coordinator = await self._setup_integration()
        cameras = await setup_platform_entities(hass, entry, "camera")
        cam_entity = cameras[0]

        cached_jpeg = b"\xff\xd8\xff\xe0cached_snapshot_bytes\xff\xd9"
        coordinator.data["latest_snapshot_bytes"] = cached_jpeg

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        live_snap_called = {"count": 0}

        def _fake_capture_snapshot(output_path: str, timeout_sec: float = 15.0):
            live_snap_called["count"] += 1
            write_synthetic_bowl_jpeg(output_path, is_ir=False, kibble_fill=0.1)
            return True

        client.capture_snapshot = mock.MagicMock(side_effect=_fake_capture_snapshot)

        # Simulate coordinator busy / session lock held during verification
        coordinator.data["summary"]["busy"] = True
        lock = getattr(coordinator, "_session_lock", None) or getattr(coordinator, "session_lock", None) or getattr(coordinator, "_lock", None)
        if isinstance(lock, asyncio.Lock):
            await lock.acquire()
        try:
            img_while_busy = await cam_entity.async_camera_image()
            self.assertEqual(img_while_busy, cached_jpeg)
            self.assertEqual(live_snap_called["count"], 0, "Live snapshot must not run while session is busy")
        finally:
            if isinstance(lock, asyncio.Lock) and lock.locked():
                lock.release()
            coordinator.data["summary"]["busy"] = False

        # Once session lock is free, live capture succeeds and updates latest_snapshot_bytes
        fresh = await coordinator.async_capture_snapshot()
        self.assertIsNotNone(fresh)
        self.assertTrue(fresh.startswith(b"\xff\xd8"))
        self.assertEqual(live_snap_called["count"], 1)

    # -----------------------------------------------------------------------
    # T3_11: Consecutive Missed Feeds Escalation + Speaker TTS Alert (F6 + F9 + F10 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_11_consecutive_missed_feeds_escalation_triggers_speaker_tts_alert(self):
        """F6 + F9 + F10 + F13: First motor_no_kibble miss is non-urgent without TTS; second consecutive miss escalates with TTS."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_CAMERA: True,
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
                const.CONF_ENABLE_TTS: True,
                const.CONF_ALERT_MODE: "every_miss",
            }
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        db = getattr(coordinator, "db", None)

        def _clog_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            out_wav = kwargs.get("output_wav")
            if out_wav:
                write_synthetic_dispense_wav(out_wav, motor_sec=3.0, with_tings=False)
            return {
                "video_ok": False,
                "audio_ok": True,
                "before_ok": False,
                "after_ok": False,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_clog_capture)
        client.speak_tts = mock.MagicMock(return_value={"ok": True, "frames_sent": 25})

        # 1st miss -> consecutive_missed_feeds == 1 (heads-up only, no speaker TTS)
        res1 = await coordinator.async_verify_feed(portions=1, trigger="manual_dispense_verify")
        self.assertEqual(res1.get("outcome"), "motor_no_kibble")
        self.assertEqual(db.consecutive_missed_feeds(), 1)
        self.assertEqual(client.speak_tts.call_count, 0)

        # 2nd consecutive miss -> consecutive_missed_feeds == 2 (escalates to clog alert + speaker TTS)
        res2 = await coordinator.async_verify_feed(portions=1, trigger="manual_dispense_verify")
        self.assertEqual(res2.get("outcome"), "motor_no_kibble")
        self.assertEqual(db.consecutive_missed_feeds(), 2)
        self.assertTrue(coordinator.data["summary"]["clog_detected"])
        self.assertGreaterEqual(client.speak_tts.call_count, 1)

    # -----------------------------------------------------------------------
    # T3_12: Play TTS Service with Chime Prepend + mulaw reqId=7 Framing (F6 + F13 + F14)
    # -----------------------------------------------------------------------
    async def test_t3_12_play_tts_service_with_chime_encodes_mulaw_reqid7(self):
        """F6 + F13 + F14: `play_tts` service with `prepend_chime=True` frames KCP conv=2 packets with reqId=7 and codec=0x86."""
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.core.molly.imm_codec import (
            _IMM_MEDIA_MAGIC,
            _TALK_CODEC_G711A,
            _imm_build_ctrl,
            _imm_build_talk_frame,
        )
        from custom_components.molly_cody_feeder.core.molly.speaker import SpeakerMixin

        # Verify protocol framing invariants (reqId=7 matching TALK_OPEN_CMD=8, codec=0x86, mulaw 0xFF silence)
        self.assertEqual(SpeakerMixin.TALK_REQ_ID, 7)
        self.assertEqual(SpeakerMixin.TALK_OPEN_CMD, 8)
        self.assertEqual(SpeakerMixin.TALK_CONV, 2)
        self.assertEqual(SpeakerMixin.TALK_FFMPEG_FORMAT, "mulaw")
        self.assertEqual(SpeakerMixin.TALK_SILENCE_BYTE, b"\xff")

        ctrl_pkt = _imm_build_ctrl(
            SpeakerMixin.TALK_REQ_ID,
            SpeakerMixin.TALK_OPEN_CMD,
            SpeakerMixin.TALK_OPEN_PAYLOAD,
        )
        magic, req_id, flags, cmd, plen = struct.unpack_from("<IIIII", ctrl_pkt, 0)
        self.assertEqual((magic, req_id, flags, cmd, plen), (_IMM_MEDIA_MAGIC, 7, 0, 8, 8))

        sample_payload = SpeakerMixin.TALK_SILENCE_BYTE * SpeakerMixin.TALK_FRAME_BYTES
        talk_pkt = _imm_build_talk_frame(1, 320, 123456, sample_payload, req_id=SpeakerMixin.TALK_REQ_ID)
        t_magic, t_req_id, t_codec, t_rate, t_bits, t_ch, t_ts, t_len = struct.unpack_from("<IIIIIIII", talk_pkt, 0)
        self.assertEqual(t_magic, _IMM_MEDIA_MAGIC)
        self.assertEqual(t_req_id, 7)
        self.assertEqual(t_codec, _TALK_CODEC_G711A)
        self.assertEqual((t_rate, t_bits, t_ch, t_len), (0, 1, 0, 320))
        self.assertEqual(talk_pkt[32:], sample_payload)

        # Verify end-to-end service routing through coordinator to client.speak_tts
        hass, entry, coordinator = await self._setup_integration()
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.speak_tts = mock.MagicMock(
            return_value={"ok": True, "frames_sent": 40, "text": "Come eat Molly", "tts_engine": "cache"}
        )

        await call_ha_service(
            hass,
            const.DOMAIN,
            const.SERVICE_PLAY_TTS,
            {
                "message": "Come eat Molly",
                "voice": "en-US-AriaNeural",
                "volume": 2.8,
                "repeat": 2,
                "prepend_chime": True,
            },
        )
        client.speak_tts.assert_called_once()
        call_kw = client.speak_tts.call_args.kwargs
        call_args = client.speak_tts.call_args.args
        text_arg = call_kw.get("text") or call_kw.get("message") or (call_args[0] if call_args else "")
        self.assertEqual(text_arg, "Come eat Molly")
        self.assertTrue(call_kw.get("prepend_chime", False))

    # -----------------------------------------------------------------------
    # T3_13: Acoustic Analyzer + SQLite Learned Baselines + weak_flow Sensor (F7 + F10 + F13 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_13_acoustic_analyzer_with_sqlite_learned_baselines_sets_weak_flow_sensor(self):
        """F7 + F10 + F13 + F15: Learned baselines from SQLite + >= 8.0s weak-ting run triggers weak_flow BinarySensor."""
        hass, entry, coordinator = await self._setup_integration()
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")
        weak_flow_bs = find_entity(binary_sensors, "weak_flow", "weak_flow_warning")

        db = getattr(coordinator, "db", None)
        # Seed 4 historical verified feeds so learned_baselines() has >= 3 samples
        for _ in range(4):
            record_db_feed(
                db,
                portions=4,
                success=True,
                clog_detected=False,
                outcome="dispensed",
                motor_duration_sec=9.2,
                kibble_rate=13.5,
            )

        baselines = db.learned_baselines()
        self.assertAlmostEqual(baselines["kibble_rate"], 13.5, places=1)
        self.assertAlmostEqual(baselines["motor_sec_per_portion"], 2.3, places=1)

        # Now run a 9.0s feed with hop-aligned weak ting flow (~3.84 tings/sec < 4.6 threshold)
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _weak_flow_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            out_wav = kwargs.get("output_wav")
            if out_wav:
                write_synthetic_dispense_wav(
                    out_wav,
                    motor_sec=9.0,
                    with_tings=True,
                    ting_spacing_sec=0.24,
                )
            return {
                "video_ok": False,
                "audio_ok": True,
                "before_ok": False,
                "after_ok": False,
                "recorded_wall_sec": 11.0,
                "audio_sec": 11.0,
                "audio_last_rx_sec": 10.95,
                "motor_window_sec": [1.0, 10.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_weak_flow_capture)
        res = await coordinator.async_verify_feed(portions=4, trigger="manual_dispense_verify")
        self.assertEqual(res.get("outcome"), "dispensed")
        self.assertTrue(coordinator.data["summary"]["weak_flow_warning"])
        self.assertTrue(weak_flow_bs.is_on)

    # -----------------------------------------------------------------------
    # T3_14: Synthetic WAV Motor Without Tings + Decision + DB Persistence (F7 + F9 + F10)
    # -----------------------------------------------------------------------
    async def test_t3_14_synthetic_wav_motor_without_tings_classifications_and_db_persistence(self):
        """F7 + F9 + F10: Motor-only WAV produces motor_no_kibble clog, increments missed streak, and does not subtract hopper."""
        from custom_components.molly_cody_feeder.core import (
            BowlAnalyzer,
            KibbleDatabase,
            classify,
            decide_passed,
            is_clog,
        )

        wav_path = write_synthetic_dispense_wav(
            self.tmp_path / "motor_only_clog.wav",
            motor_sec=3.0,
            with_tings=False,
        )
        cfg = mock.MagicMock()
        cfg.min_kibble_audio_peaks = 15
        cfg.ting_rate_min = 4.3
        analyzer = BowlAnalyzer(cfg)

        audio_res = analyzer.analyze_dispense_audio(str(wav_path))
        self.assertTrue(audio_res.motor_detected)
        self.assertGreaterEqual(audio_res.motor_duration_sec, 2.5)
        self.assertEqual(audio_res.ting_count, 0)
        self.assertFalse(audio_res.kibble_dispensed_audio)

        passed = decide_passed(audio_res, None, feed_abnormal=1, expect_dispense=True)
        outcome, passed = classify(audio_res, None, expect_dispense=True, passed=passed, cut_short=False)
        self.assertFalse(passed)
        self.assertEqual(outcome, "motor_no_kibble")
        self.assertTrue(is_clog(outcome, passed))

        db = KibbleDatabase(str(self.tmp_path / "t3_14.db"))
        db.record_refill(200, "Full hopper")
        record_db_feed(
            db,
            event_type="scheduled_verify",
            portions=5,
            success=False,
            clog_detected=True,
            audio_state="motor_only_clog",
            motor_detected=True,
            kibble_peaks=0,
            outcome="motor_no_kibble",
            motor_duration_sec=audio_res.motor_duration_sec,
            kibble_rate=0.0,
        )
        self.assertEqual(db.consecutive_missed_feeds(), 1)
        hop = db.hopper_status(200)
        self.assertEqual(hop["portions_used"], 0, "motor_no_kibble must not subtract portions from hopper")
        self.assertEqual(hop["portions_left"], 200)

    # -----------------------------------------------------------------------
    # T3_15: Visual Bowl Analyzer + Annotated/Before/After Image Entities (F8 + F13 + F17)
    # -----------------------------------------------------------------------
    async def test_t3_15_visual_bowl_analyzer_updates_before_after_annotated_image_entities(self):
        """F8 + F13 + F17: Verified feed with vision enabled populates Before, After, and Annotated Bowl ImageEntities."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_CAMERA: True,
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: True,
            }
        )
        images = await setup_platform_entities(hass, entry, "image")
        self.assertEqual(len(images), 3)

        before_ent = find_entity(images, "before_feed", "before", "Before Feed")
        after_ent = find_entity(images, "after_feed", "after", "After Feed")
        annotated_ent = find_entity(images, "annotated_bowl", "annotated", "Annotated Bowl")

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _capture_with_images(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            if kwargs.get("output_wav"):
                write_synthetic_dispense_wav(kwargs["output_wav"], motor_sec=3.0, with_tings=True)
            if kwargs.get("before_jpg"):
                write_synthetic_bowl_jpeg(kwargs["before_jpg"], is_ir=False, kibble_fill=0.0)
            if kwargs.get("after_jpg"):
                write_synthetic_bowl_jpeg(kwargs["after_jpg"], is_ir=False, kibble_fill=0.35)
            return {
                "video_ok": True,
                "audio_ok": True,
                "before_ok": True,
                "after_ok": True,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_capture_with_images)
        res = await coordinator.async_verify_feed(portions=2, trigger="manual_dispense_verify")
        self.assertEqual(res.get("outcome"), "dispensed")

        before_bytes = await before_ent.async_image()
        after_bytes = await after_ent.async_image()
        annotated_bytes = await annotated_ent.async_image()

        for label, img_bytes in (
            ("before", before_bytes),
            ("after", after_bytes),
            ("annotated", annotated_bytes),
        ):
            self.assertIsNotNone(img_bytes, f"{label} ImageEntity must return bytes after verified feed")
            self.assertTrue(img_bytes.startswith(b"\xff\xd8"), f"{label} ImageEntity must contain valid JPEG header")

    # -----------------------------------------------------------------------
    # T3_16: Daylight Verified Meal + Post-Meal Eaten Check (F8 + F13 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_16_daylight_verified_meal_schedules_and_runs_post_meal_eaten_check(self):
        """F8 + F13 + F15: Post-meal eaten check compares fresh bowl fill against 0.7 * after_meal_fill."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={const.CONF_EATEN_CHECK_MIN: 60}
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        # Case A: Bowl still full (fill ~0.30 >= 0.7 * 0.25) -> not eaten, fires food_not_eaten alert
        def _snap_still_full(output_path: str, timeout_sec: float = 15.0):
            write_synthetic_bowl_jpeg(output_path, is_ir=False, kibble_fill=0.35)
            return True

        client.capture_snapshot = mock.MagicMock(side_effect=_snap_still_full)
        eaten_fn = (
            getattr(coordinator, "async_run_eaten_check", None)
            or getattr(coordinator, "async_check_food_eaten", None)
            or getattr(coordinator, "check_food_eaten", None)
        )
        self.assertIsNotNone(eaten_fn, "Coordinator must expose eaten check method")
        res_not_eaten = eaten_fn(0.25)
        if inspect.isawaitable(res_not_eaten):
            res_not_eaten = await res_not_eaten
        self.assertFalse(res_not_eaten)

        alert_events = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_ALERT]
        self.assertGreaterEqual(len(alert_events), 1)

        # Case B: Bowl now empty (fill 0.0 < 0.7 * 0.25) -> eaten is True
        def _snap_eaten(output_path: str, timeout_sec: float = 15.0):
            write_synthetic_bowl_jpeg(output_path, is_ir=False, kibble_fill=0.0)
            return True

        client.capture_snapshot = mock.MagicMock(side_effect=_snap_eaten)
        res_eaten = eaten_fn(0.25)
        if inspect.isawaitable(res_eaten):
            res_eaten = await res_eaten
        self.assertTrue(res_eaten)

    # -----------------------------------------------------------------------
    # T3_17: recording_cut_short + Missed Streak + Auto-Retry Guard (F5 + F9 + F10 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_17_recording_cut_short_neither_increments_missed_streak_nor_triggers_auto_retry(self):
        """F5 + F9 + F10 + F13: Stream drop (`audio_sec < 0.75 * wall`) classifies recording_cut_short, never increments streak or retries."""
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.core import recording_cut_short

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_AUTO_RETRY: True,
            }
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        db = getattr(coordinator, "db", None)

        cut_session = {
            "video_ok": False,
            "audio_ok": True,
            "before_ok": False,
            "after_ok": False,
            "recorded_wall_sec": 40.0,
            "audio_sec": 3.5,
            "audio_last_rx_sec": 3.6,
            "motor_window_sec": None,
            "recording_extended_sec": 0.0,
        }
        self.assertTrue(recording_cut_short(cut_session))

        def _cut_short_capture(**kwargs):
            out_wav = kwargs.get("output_wav")
            if out_wav:
                write_synthetic_dispense_wav(out_wav, motor_sec=0.0, with_tings=False)
            return dict(cut_session)

        client.capture_av_clip = mock.MagicMock(side_effect=_cut_short_capture)
        client.dispense_food = mock.MagicMock(return_value=(True, "cloud"))

        sched_fn = (
            getattr(coordinator, "async_run_scheduled_meal_check", None)
            or getattr(coordinator, "async_run_scheduled_check", None)
            or getattr(coordinator, "run_scheduled_check", None)
        )
        if callable(sched_fn):
            r = sched_fn(2)
            if inspect.isawaitable(r):
                await r
        else:
            res = await coordinator.async_verify_feed(portions=2, trigger="scheduled_verify")
            self.assertEqual(res.get("outcome"), "recording_cut_short")

        self.assertEqual(db.consecutive_missed_feeds(), 0)
        self.assertEqual(db.portions_today("auto_retry_verify"), 0)
        self.assertFalse(coordinator.data["summary"]["clog_detected"])

    # -----------------------------------------------------------------------
    # T3_18: FeedVerifier + KibbleDatabase + Coordinator Event Bus + EventEntity (F9 + F10 + F13 + F17)
    # -----------------------------------------------------------------------
    async def test_t3_18_feed_verifier_end_to_end_persists_db_and_fires_ha_event(self):
        """F9 + F10 + F13 + F17: Verified feed persists SQLite row, fires EVENT_FEED, and updates feed_event EventEntity."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration()
        events = await setup_platform_entities(hass, entry, "event")
        feed_event_ent = find_entity(events, "feed_event", "feed")

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _good_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            if kwargs.get("output_wav"):
                write_synthetic_dispense_wav(kwargs["output_wav"], motor_sec=3.0, with_tings=True)
            if kwargs.get("before_jpg"):
                write_synthetic_bowl_jpeg(kwargs["before_jpg"], is_ir=False, kibble_fill=0.0)
            if kwargs.get("after_jpg"):
                write_synthetic_bowl_jpeg(kwargs["after_jpg"], is_ir=False, kibble_fill=0.25)
            return {
                "video_ok": True,
                "audio_ok": True,
                "before_ok": True,
                "after_ok": True,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_good_capture)
        result = await coordinator.async_verify_feed(portions=3, trigger="manual_dispense_verify")

        self.assertEqual(result.get("outcome"), "dispensed")
        db = getattr(coordinator, "db", None)
        recent = db.recent_events(limit=5) if hasattr(db, "recent_events") else db.get_recent_events(limit=5)
        self.assertGreaterEqual(len(recent), 1)
        self.assertEqual(recent[0]["portions"], 3)
        self.assertEqual(recent[0]["outcome"], "dispensed")
        self.assertEqual(int(recent[0]["success"]), 1)

        fired_feed = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_FEED]
        self.assertGreaterEqual(len(fired_feed), 1)
        self.assertEqual(fired_feed[-1].get("outcome"), "dispensed")
        self.assertIsNotNone(coordinator.data.get("last_feed_event"))
        self.assertIsNotNone(feed_event_ent)

    # -----------------------------------------------------------------------
    # T3_19: Refill Hopper Service + SQLite DB + Hopper Sensors & Low BinarySensor (F10 + F14 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_19_refill_hopper_service_resets_hopper_sensors_and_clears_hopper_low(self):
        """F10 + F14 + F15: Depleting hopper <= 20% turns on hopper_low BinarySensor; refill_hopper service resets to 100%."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_HOPPER_CAPACITY: 100,
                const.CONF_HOPPER_LOW_PCT: 20.0,
            }
        )
        sensors = await setup_platform_entities(hass, entry, "sensor")
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")

        portions_left_sensor = find_entity(sensors, "hopper_portions_left", "portions_left")
        hopper_pct_sensor = find_entity(sensors, "hopper_percent", "hopper_level", "percent_left")
        hopper_low_bs = find_entity(binary_sensors, "hopper_low", "low_hopper")

        db = getattr(coordinator, "db", None)
        db.record_refill(100, "Initial 100p")
        record_db_feed(db, portions=85, success=True, outcome="dispensed")

        await coordinator.async_refresh()
        self.assertEqual(int(entity_sensor_value(portions_left_sensor)), 15)
        self.assertAlmostEqual(float(entity_sensor_value(hopper_pct_sensor)), 15.0, places=1)
        self.assertTrue(hopper_low_bs.is_on)

        # Call refill_hopper service
        await call_ha_service(
            hass,
            const.DOMAIN,
            const.SERVICE_REFILL_HOPPER,
            {"capacity_portions": 100, "note": "Topped off"},
        )

        self.assertEqual(int(entity_sensor_value(portions_left_sensor)), 100)
        self.assertAlmostEqual(float(entity_sensor_value(hopper_pct_sensor)), 100.0, places=1)
        self.assertFalse(hopper_low_bs.is_on)

    # -----------------------------------------------------------------------
    # T3_20: RetentionManager Prune + JSONL ML Export + Protected Snapshot (F10 + F11)
    # -----------------------------------------------------------------------
    async def test_t3_20_retention_manager_exports_ml_jsonl_before_pruning_expired_captures(self):
        """F10 + F11: RetentionManager exports expired event metrics to JSONL before deleting media, keeping DB rows & protected files."""
        from custom_components.molly_cody_feeder.core import KibbleDatabase, RetentionManager

        captures_dir = self.tmp_path / "captures"
        captures_dir.mkdir(parents=True, exist_ok=True)
        ml_jsonl = self.tmp_path / "ml_export.jsonl"
        protected_snap = captures_dir / "latest_annotated.jpg"
        protected_snap.write_bytes(b"\xff\xd8protected\xff\xd9")

        old_mp4 = captures_dir / "old_feed.mp4"
        old_wav = captures_dir / "old_feed.wav"
        old_before = captures_dir / "old_before.jpg"
        old_after = captures_dir / "old_after.jpg"
        old_ann = captures_dir / "old_ann.jpg"
        for f in (old_mp4, old_wav, old_before, old_after, old_ann):
            f.write_bytes(b"media_payload_bytes")

        db = KibbleDatabase(str(self.tmp_path / "retention.db"))
        event_id = record_db_feed(
            db,
            portions=2,
            success=True,
            clog_detected=False,
            outcome="dispensed",
            motor_duration_sec=4.6,
            kibble_rate=12.5,
            before_image=str(old_before),
            after_image=str(old_after),
            annotated_image=str(old_ann),
            video_clip=str(old_mp4),
            audio_clip=str(old_wav),
        )
        # Backdate event timestamp by 10 days
        ten_days_ago = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
        conn = db._connect()
        try:
            with conn:
                conn.execute("UPDATE feed_events SET timestamp = ? WHERE id = ?", (ten_days_ago, event_id))
        finally:
            conn.close()

        cfg = mock.MagicMock()
        cfg.keep_footage = True
        cfg.capture_retention_days = 7
        cfg.keep_failed_footage_days = 14
        cfg.video_keep_days_ok = 0
        cfg.capture_max_mb = 0
        cfg.db_retention_days = 0
        cfg.export_ml_on_prune = True
        cfg.ml_export_path = str(ml_jsonl)

        retention = RetentionManager(cfg, db, captures_dir, protected=[protected_snap])
        stats = retention.run() if hasattr(retention, "run") else retention.prune()

        self.assertGreaterEqual(stats.get("events_purged", 0), 1)
        self.assertGreaterEqual(stats.get("exported", 0), 1)
        self.assertTrue(ml_jsonl.exists(), "ML JSONL file must be written before media deletion")

        exported_rows = [json.loads(line) for line in ml_jsonl.read_text().splitlines() if line.strip()]
        self.assertEqual(len(exported_rows), 1)
        self.assertEqual(exported_rows[0]["event_id"], event_id)
        self.assertEqual(exported_rows[0]["outcome"], "dispensed")

        # Expired media files deleted, protected file and SQLite row preserved
        for f in (old_mp4, old_wav, old_before, old_after, old_ann):
            self.assertFalse(f.exists(), f"Expired file {f} should have been pruned")
        self.assertTrue(protected_snap.exists(), "Protected latest_annotated.jpg must never be deleted")
        self.assertEqual(db.get_stats()["total_events"], 1, "SQLite metric row must be preserved after media prune")

    # -----------------------------------------------------------------------
    # T3_21: RuntimeConfig.from_entry Merges Data + Options Into Isolated Storage (F11 + F12 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_21_runtime_config_merges_entry_data_and_options_into_isolated_storage(self):
        """F11 + F12 + F13: RuntimeConfig.from_entry merges entry.data + entry.options and isolates storage paths."""
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.core import RuntimeConfig

        hass, entry = make_hass_and_entry(
            self.tmp_path,
            entry_id="entry_isolated_21",
            data={
                const.CONF_DEVICE_ID: "dev_21",
                const.CONF_CONNECTION_MODE: "auto",
                const.CONF_HOPPER_CAPACITY: 240,
                const.CONF_ENABLE_CAMERA: True,
                const.CONF_ENABLE_VISION_VERIFICATION: True,
            },
            options={
                const.CONF_CONNECTION_MODE: "lan",
                const.CONF_HOPPER_CAPACITY: 320,
                const.CONF_ENABLE_CAMERA: False,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
            },
        )
        rt = RuntimeConfig.from_entry(hass, entry)

        mode_val = getattr(rt, "connection_mode", None) or getattr(rt, "feeder_mode", None)
        self.assertEqual(mode_val, "lan")
        self.assertEqual(getattr(rt, "hopper_capacity_portions", None), 320)
        self.assertFalse(getattr(rt, "enable_camera", True))
        self.assertFalse(getattr(rt, "enable_vision_verification", True))

        expected_storage_dir = Path(hass.config.path(".storage", const.DOMAIN, "entry_isolated_21"))
        for attr in ("session_file", "db_path", "captures_dir", "tts_cache_dir"):
            p = Path(getattr(rt, attr))
            self.assertTrue(
                str(p).startswith(str(expected_storage_dir)),
                f"{attr} ({p}) must reside inside {expected_storage_dir}",
            )

    # -----------------------------------------------------------------------
    # T3_22: OptionsFlow Live Toggle Update Disables Camera & Vision at Runtime (F12 + F13 + F17)
    # -----------------------------------------------------------------------
    async def test_t3_22_options_flow_live_toggle_update_disables_camera_and_vision_at_runtime(self):
        """F12 + F13 + F17: Updating modular compute toggles via OptionsFlow / coordinator disables camera & vision at runtime."""
        from custom_components.molly_cody_feeder import config_flow as cf_mod
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_CAMERA: True,
                const.CONF_ENABLE_VISION_VERIFICATION: True,
                const.CONF_ENABLE_TTS: True,
            }
        )
        cameras = await setup_platform_entities(hass, entry, "camera")
        cam_entity = cameras[0]

        # Exercise OptionsFlow step_init
        flow_cls = getattr(cf_mod, "MollyCodyConfigFlow", None) or getattr(cf_mod, "ConfigFlow")
        if hasattr(flow_cls, "async_get_options_flow"):
            opt_flow = flow_cls.async_get_options_flow(entry)
        else:
            opt_cls = getattr(cf_mod, "MollyCodyOptionsFlow", None) or getattr(cf_mod, "OptionsFlowHandler")
            opt_flow = opt_cls(entry)
        opt_flow.hass = hass

        new_opts = dict(entry.options)
        new_opts[const.CONF_ENABLE_CAMERA] = False
        new_opts[const.CONF_ENABLE_VISION_VERIFICATION] = False
        new_opts[const.CONF_ENABLE_TTS] = False

        res = await opt_flow.async_step_init(new_opts)
        self.assertEqual(res.get("type"), "create_entry")

        await coordinator.async_set_option(const.CONF_ENABLE_CAMERA, False)
        await coordinator.async_set_option(const.CONF_ENABLE_VISION_VERIFICATION, False)
        await coordinator.async_set_option(const.CONF_ENABLE_TTS, False)

        self.assertFalse(coordinator.data["options"][const.CONF_ENABLE_CAMERA])
        self.assertFalse(coordinator.data["options"][const.CONF_ENABLE_VISION_VERIFICATION])

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.capture_snapshot = mock.MagicMock(
            side_effect=AssertionError("capture_snapshot must not be called when enable_camera=False")
        )
        snap = await coordinator.async_capture_snapshot()
        self.assertIsNone(snap)
        cam_img = await cam_entity.async_camera_image()
        self.assertEqual(cam_img, coordinator.data.get("latest_snapshot_bytes"))

    # -----------------------------------------------------------------------
    # T3_23: Scheduled Meal Auto-Retry Daily Cap & hopper_empty_suspected Stop (F9 + F10 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_23_scheduled_meal_auto_retry_respects_daily_cap_and_hopper_empty_stop(self):
        """F9 + F10 + F13: Scheduled meal + retry both motor_no_kibble fires hopper_empty_suspected; daily cap blocks excess retries."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
                const.CONF_ENABLE_AUTO_RETRY: True,
                const.CONF_MAX_DAILY_RETRY: 2,
            }
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        db = getattr(coordinator, "db", None)

        def _clog_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            out_wav = kwargs.get("output_wav")
            if out_wav:
                write_synthetic_dispense_wav(out_wav, motor_sec=3.0, with_tings=False)
            return {
                "video_ok": False,
                "audio_ok": True,
                "before_ok": False,
                "after_ok": False,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_clog_capture)

        sched_fn = (
            getattr(coordinator, "async_run_scheduled_meal_check", None)
            or getattr(coordinator, "async_run_scheduled_check", None)
            or getattr(coordinator, "run_scheduled_check", None)
        )
        self.assertIsNotNone(sched_fn, "Coordinator must implement scheduled meal check with auto-retry policy")
        r = sched_fn(2)
        if inspect.isawaitable(r):
            await r

        # Both scheduled meal and 1st retry had motor_no_kibble -> stops after 1 retry with hopper_empty_suspected
        self.assertEqual(db.portions_today("auto_retry_verify"), 1)
        alert_events = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_ALERT]
        self.assertTrue(
            any("hopper_empty" in str(ev).lower() or "empty" in str(ev).lower() for ev in alert_events),
            f"Expected hopper_empty_suspected alert in {alert_events!r}",
        )

        # Exhaust daily cap (2 portions) and verify next scheduled check does not dispense a 3rd retry portion
        record_db_feed(
            db,
            event_type="auto_retry_verify",
            portions=1,
            success=False,
            clog_detected=True,
            outcome="no_motor",
        )
        self.assertEqual(db.portions_today("auto_retry_verify"), 2)

        r2 = sched_fn(2)
        if inspect.isawaitable(r2):
            await r2
        self.assertEqual(db.portions_today("auto_retry_verify"), 2, "Daily retry cap must block a 3rd retry portion")

    # -----------------------------------------------------------------------
    # T3_24: Custom Services `set_dp` & `clear_clog_alert` Update Entities (F3 + F14 + F15 + F16)
    # -----------------------------------------------------------------------
    async def test_t3_24_custom_services_set_dp_and_clear_clog_alert_update_entities(self):
        """F3 + F14 + F15 + F16: Calling set_dp(101, False) and clear_clog_alert updates Switch and BinarySensor entities."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(dps={"101": True})
        switches = await setup_platform_entities(hass, entry, "switch")
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")

        dp101_switch = find_entity(switches, "indicator_light", "dp_101", "101")
        clog_bs = find_entity(binary_sensors, "clog_detected", "clog")

        self.assertTrue(dp101_switch.is_on)

        # Call set_dp service to turn off DP 101 indicator light
        await call_ha_service(
            hass,
            const.DOMAIN,
            const.SERVICE_SET_DP,
            {"dp_id": 101, "value": False},
        )
        self.assertFalse(dp101_switch.is_on)
        self.assertEqual(coordinator.data["dps"].get("101"), False)

        # Simulate active clog alert, then clear it via clear_clog_alert service
        coordinator.data["summary"]["clog_detected"] = True
        coordinator.data["summary"]["status"] = "problem"
        self.assertTrue(clog_bs.is_on)

        await call_ha_service(hass, const.DOMAIN, const.SERVICE_CLEAR_CLOG_ALERT, {})
        self.assertFalse(coordinator.data["summary"]["clog_detected"])
        self.assertFalse(clog_bs.is_on)

    # -----------------------------------------------------------------------
    # T3_25: Verified Feed Updates All 22 Sensors & 9 Binary Sensors (F13 + F15)
    # -----------------------------------------------------------------------
    async def test_t3_25_verified_feed_updates_all_summary_sensors_and_binary_sensors(self):
        """F13 + F15: Completing a verified 4-portion feed updates all summary SensorEntity and BinarySensorEntity states."""
        hass, entry, coordinator = await self._setup_integration()
        sensors = await setup_platform_entities(hass, entry, "sensor")
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")

        self.assertGreaterEqual(len(sensors), 22)
        self.assertGreaterEqual(len(binary_sensors), 9)

        await coordinator.async_refill_hopper(capacity_portions=240, note="Fresh refill")

        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _good_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            if kwargs.get("output_wav"):
                write_synthetic_dispense_wav(kwargs["output_wav"], motor_sec=4.0, with_tings=True)
            if kwargs.get("before_jpg"):
                write_synthetic_bowl_jpeg(kwargs["before_jpg"], is_ir=False, kibble_fill=0.0)
            if kwargs.get("after_jpg"):
                write_synthetic_bowl_jpeg(kwargs["after_jpg"], is_ir=False, kibble_fill=0.22)
            return {
                "video_ok": True,
                "audio_ok": True,
                "before_ok": True,
                "after_ok": True,
                "recorded_wall_sec": 6.0,
                "audio_sec": 6.0,
                "audio_last_rx_sec": 5.95,
                "motor_window_sec": [1.0, 5.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_good_capture)
        await coordinator.async_verify_feed(portions=4, trigger="manual_dispense_verify")

        today_meals_s = find_entity(sensors, "today_meals")
        today_fed_s = find_entity(sensors, "today_fed")
        today_portions_s = find_entity(sensors, "today_portions")
        last_portions_s = find_entity(sensors, "last_meal_portions")
        last_result_s = find_entity(sensors, "last_meal_result")
        hopper_left_s = find_entity(sensors, "hopper_portions_left", "portions_left")
        motor_dur_s = find_entity(sensors, "last_motor_duration_sec", "motor_duration")

        self.assertEqual(int(entity_sensor_value(today_meals_s)), 1)
        self.assertEqual(int(entity_sensor_value(today_fed_s)), 1)
        self.assertEqual(int(entity_sensor_value(today_portions_s)), 4)
        self.assertEqual(int(entity_sensor_value(last_portions_s)), 4)
        self.assertEqual(entity_sensor_value(last_result_s), "fed")
        self.assertEqual(int(entity_sensor_value(hopper_left_s)), 236)
        self.assertGreater(float(entity_sensor_value(motor_dur_s)), 3.0)

        online_bs = find_entity(binary_sensors, "online")
        clog_bs = find_entity(binary_sensors, "clog_detected", "clog")
        self.assertTrue(online_bs.is_on)
        self.assertFalse(clog_bs.is_on)

    # -----------------------------------------------------------------------
    # T3_26: Button Entities Press Invokes Coordinator Actions (F13 + F16)
    # -----------------------------------------------------------------------
    async def test_t3_26_button_entities_press_invokes_coordinator_actions_and_refreshes(self):
        """F13 + F16: Pressing all 8 ButtonEntity instances invokes the corresponding coordinator action methods."""
        hass, entry, coordinator = await self._setup_integration()
        buttons = await setup_platform_entities(hass, entry, "button")
        self.assertGreaterEqual(len(buttons), 8)

        with mock.patch.object(coordinator, "async_feed", new=mock.AsyncMock(return_value={"ok": True})) as m_feed, \
             mock.patch.object(coordinator, "async_verify_feed", new=mock.AsyncMock(return_value={"ok": True})) as m_verify, \
             mock.patch.object(coordinator, "async_check_bowl", new=mock.AsyncMock(return_value={"ok": True})) as m_bowl, \
             mock.patch.object(coordinator, "async_capture_snapshot", new=mock.AsyncMock(return_value=b"\xff\xd8")) as m_snap, \
             mock.patch.object(coordinator, "async_refill_hopper", new=mock.AsyncMock(return_value={"ok": True})) as m_refill, \
             mock.patch.object(coordinator, "async_play_chime", new=mock.AsyncMock(return_value={"ok": True})) as m_chime, \
             mock.patch.object(coordinator, "async_sync_schedules", new=mock.AsyncMock(return_value=[])) as m_sync, \
             mock.patch.object(coordinator, "async_clear_clog_alert", new=mock.AsyncMock(return_value=None)) as m_clear:

            await find_entity(buttons, "feed_1_portion", "feed_1p", "feed", "Feed 1p").async_press()
            m_feed.assert_awaited_once()

            await find_entity(buttons, "verify_feed", "feed_and_verify", "Feed & Verify").async_press()
            m_verify.assert_awaited_once()

            await find_entity(buttons, "check_bowl", "Check Bowl").async_press()
            m_bowl.assert_awaited_once()

            await find_entity(buttons, "capture_snapshot", "snapshot", "Snapshot").async_press()
            m_snap.assert_awaited_once()

            await find_entity(buttons, "refill_hopper", "Refill Hopper").async_press()
            m_refill.assert_awaited_once()

            await find_entity(buttons, "play_chime", "chime", "Chime").async_press()
            m_chime.assert_awaited_once()

            await find_entity(buttons, "sync_schedules", "sync_schedule", "Sync Schedule").async_press()
            m_sync.assert_awaited_once()

            await find_entity(buttons, "clear_clog_alert", "clear_alert", "Clear Alert").async_press()
            m_clear.assert_awaited_once()

    # -----------------------------------------------------------------------
    # T3_27: No-Food Timer Alert Mode + Food Observations + Alert Event (F8 + F10 + F13)
    # -----------------------------------------------------------------------
    async def test_t3_27_no_food_timer_alert_mode_combines_vision_food_observations_and_alerts(self):
        """F8 + F10 + F13: In alert_mode='no_food_hours', expired food evidence fires alert; fresh bowl food resets timer."""
        from custom_components.molly_cody_feeder import const

        hass, entry, coordinator = await self._setup_integration(
            options={
                const.CONF_ALERT_MODE: "no_food_hours",
                const.CONF_NO_FOOD_HOURS: 8.0,
            }
        )
        db = getattr(coordinator, "db", None)
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        # Insert food observation backdated 9.5 hours ago
        nine_hours_ago = (datetime.now() - timedelta(hours=9, minutes=30)).isoformat(timespec="seconds")
        conn = db._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO food_observations (timestamp, source, detail) VALUES (?, ?, ?)",
                    (nine_hours_ago, "ting", "backdated meal"),
                )
        finally:
            conn.close()

        # Snapshot shows empty bowl -> last evidence stays 9.5h old -> fires no_food_timer alert
        def _empty_snap(output_path: str, timeout_sec: float = 15.0):
            write_synthetic_bowl_jpeg(output_path, is_ir=False, kibble_fill=0.0)
            return True

        client.capture_snapshot = mock.MagicMock(side_effect=_empty_snap)
        timer_fn = (
            getattr(coordinator, "async_check_no_food_timer", None)
            or getattr(coordinator, "check_no_food_timer", None)
        )
        self.assertIsNotNone(timer_fn, "Coordinator must expose no-food timer check method")
        hours = timer_fn()
        if inspect.isawaitable(hours):
            hours = await hours
        self.assertIsNotNone(hours)
        self.assertGreaterEqual(float(hours), 8.0)

        alert_events = [ev for ev_type, ev in hass._fired_events if ev_type == const.EVENT_ALERT]
        self.assertGreaterEqual(len(alert_events), 1)

        # Now snapshot sees kibble in the bowl -> records fresh 'camera' evidence -> hours drops near 0
        def _full_snap(output_path: str, timeout_sec: float = 15.0):
            write_synthetic_bowl_jpeg(output_path, is_ir=False, kibble_fill=0.35)
            return True

        client.capture_snapshot = mock.MagicMock(side_effect=_full_snap)
        hours_after = timer_fn()
        if inspect.isawaitable(hours_after):
            hours_after = await hours_after
        self.assertLess(float(hours_after), 0.2)
        self.assertEqual(db.last_food_evidence()["source"], "camera")

    # -----------------------------------------------------------------------
    # T3_28: Blueprints & Lovelace YAML Parity with Events & Services (F14 + F15 + F16 + F18)
    # -----------------------------------------------------------------------
    async def test_t3_28_blueprints_and_lovelace_yaml_match_integration_events_and_services(self):
        """F14 + F15 + F16 + F18: Blueprints, Lovelace YAML, services.yaml, and manifest.json match const.py contracts."""
        from custom_components.molly_cody_feeder import const

        class HaYamlLoader(yaml.SafeLoader):
            pass

        HaYamlLoader.add_constructor("!input", lambda loader, node: f"!input:{loader.construct_scalar(node)}")

        manifest_path = ROOT / "custom_components" / "molly_cody_feeder" / "manifest.json"
        services_yaml_path = ROOT / "custom_components" / "molly_cody_feeder" / "services.yaml"
        lovelace_path = ROOT / "lovelace" / "molly_cody_dashboard.yaml"
        blueprints_dir = ROOT / "blueprints" / "automation" / "molly_cody_feeder"

        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["domain"], const.DOMAIN)
        self.assertEqual(manifest["version"], const.VERSION)
        self.assertTrue(manifest["config_flow"])

        services_doc = yaml.load(services_yaml_path.read_text(), Loader=HaYamlLoader)
        expected_services = {
            const.SERVICE_FEED,
            const.SERVICE_VERIFY_FEED,
            const.SERVICE_CHECK_BOWL,
            const.SERVICE_SET_SCHEDULE_SLOT,
            const.SERVICE_SET_SCHEDULE_RAW,
            const.SERVICE_SYNC_SCHEDULES,
            const.SERVICE_REFILL_HOPPER,
            const.SERVICE_CAPTURE_SNAPSHOT,
            const.SERVICE_PLAY_TTS,
            const.SERVICE_PLAY_AUDIO,
            const.SERVICE_PLAY_CHIME,
            const.SERVICE_SET_DP,
            const.SERVICE_RUN_ONLINE_CHECK,
            const.SERVICE_CLEAR_CLOG_ALERT,
        }
        self.assertTrue(expected_services.issubset(set(services_doc.keys())))

        lovelace_text = lovelace_path.read_text()
        lovelace_doc = yaml.load(lovelace_text, Loader=HaYamlLoader)
        self.assertIsInstance(lovelace_doc, dict)
        self.assertIn("molly_cody_feeder", lovelace_text)

        bp_files = sorted(blueprints_dir.glob("*.yaml"))
        self.assertGreaterEqual(len(bp_files), 1, "At least one automation blueprint must exist")
        for bp_file in bp_files:
            bp_raw = bp_file.read_text()
            bp_doc = yaml.load(bp_raw, Loader=HaYamlLoader)
            self.assertIn("blueprint", bp_doc)
            self.assertEqual(bp_doc["blueprint"].get("domain"), "automation")
            self.assertTrue(
                const.EVENT_ALERT in bp_raw
                or const.EVENT_FEED in bp_raw
                or const.DOMAIN in bp_raw,
                f"Blueprint {bp_file.name} must reference {const.DOMAIN} events or entities",
            )

    # -----------------------------------------------------------------------
    # T3_29: Diagnostics Secret Redaction Across Entry, Client & Coordinator (F1 + F12 + F13 + F14)
    # -----------------------------------------------------------------------
    async def test_t3_29_diagnostics_redacts_secrets_across_config_entry_client_and_coordinator(self):
        """F1 + F12 + F13 + F14: async_get_config_entry_diagnostics redacts all credentials while preserving telemetry."""
        from custom_components.molly_cody_feeder import const
        from custom_components.molly_cody_feeder.diagnostics import async_get_config_entry_diagnostics

        secret_email = "topsecret.owner@private.org"
        secret_pass = "UltraSecretPassword!99"
        secret_key = "SecretLocalKey16"
        secret_sid = "sid_secret_token_98765"
        secret_ecode = "ecode_secret_54321"
        secret_uid = "uid_secret_112233"

        hass, entry, coordinator = await self._setup_integration(
            data={
                const.CONF_EMAIL: secret_email,
                const.CONF_PASSWORD: secret_pass,
                const.CONF_LOCAL_KEY: secret_key,
            }
        )
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
        client.sid = secret_sid
        client.ecode = secret_ecode
        client.uid = secret_uid

        diag = await async_get_config_entry_diagnostics(hass, entry)
        serialized = json.dumps(diag, default=str)

        for secret in (secret_email, secret_pass, secret_key, secret_sid, secret_ecode, secret_uid):
            self.assertNotIn(secret, serialized, f"Secret value {secret!r} leaked in diagnostics output!")
        self.assertIn("**REDACTED**", serialized)
        self.assertIn("summary", serialized)

    # -----------------------------------------------------------------------
    # T3_30: Nightvision Select Entity DP 108 + IR Night Bowl Verification (F3 + F8 + F13 + F15 + F16)
    # -----------------------------------------------------------------------
    async def test_t3_30_ir_night_vision_bowl_check_and_nightvision_select_entity_dp108(self):
        """F3 + F8 + F13 + F15 + F16: Selecting IR On (DP 108='2') + IR night bowl check sets is_infrared_mode BinarySensor."""
        hass, entry, coordinator = await self._setup_integration(dps={"108": "0"})
        selects = await setup_platform_entities(hass, entry, "select")
        binary_sensors = await setup_platform_entities(hass, entry, "binary_sensor")

        nv_select = find_entity(selects, "nightvision", "night_vision", "dp_108")
        ir_bs = find_entity(binary_sensors, "is_infrared_mode", "infrared_mode", "ir_mode")
        food_bs = find_entity(binary_sensors, "food_present", "food_in_bowl")

        # Select 'On' ('2') on Nightvision SelectEntity
        on_opt = "On" if "On" in nv_select.options else ("on" if "on" in nv_select.options else "2")
        await nv_select.async_select_option(on_opt)
        self.assertEqual(str(coordinator.data["dps"].get("108")), "2")

        # Run a verified feed in IR night-vision mode with new kibble occluding the IR reflection
        client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)

        def _ir_capture(**kwargs):
            trig = kwargs.get("trigger_fn")
            if callable(trig):
                trig()
            if kwargs.get("output_wav"):
                write_synthetic_dispense_wav(kwargs["output_wav"], motor_sec=3.0, with_tings=True)
            if kwargs.get("before_jpg"):
                write_synthetic_bowl_jpeg(kwargs["before_jpg"], is_ir=True, kibble_fill=0.0)
            if kwargs.get("after_jpg"):
                write_synthetic_bowl_jpeg(kwargs["after_jpg"], is_ir=True, kibble_fill=0.45)
            return {
                "video_ok": True,
                "audio_ok": True,
                "before_ok": True,
                "after_ok": True,
                "recorded_wall_sec": 5.0,
                "audio_sec": 5.0,
                "audio_last_rx_sec": 4.95,
                "motor_window_sec": [1.0, 4.0],
                "recording_extended_sec": 0.0,
            }

        client.capture_av_clip = mock.MagicMock(side_effect=_ir_capture)
        res = await coordinator.async_verify_feed(portions=2, trigger="manual_dispense_verify")

        self.assertEqual(res.get("outcome"), "dispensed")
        self.assertTrue(coordinator.data["summary"]["is_infrared_mode"])
        self.assertTrue(ir_bs.is_on)
        self.assertTrue(food_bs.is_on)


if __name__ == "__main__":
    unittest.main()
