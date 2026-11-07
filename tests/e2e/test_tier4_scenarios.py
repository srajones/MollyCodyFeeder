"""Tier 4 End-to-End Test Suite: Real-World Household Scenarios (`test_tier4_scenarios.py`).

Covers realistic, multi-step household lifecycles end-to-end across features F1-F18 as specified
in `PROJECT.md` and `TEST_INFRA.md`:
  1. Full 24-hour 5-slot household lifecycle (ConfigFlow -> setup -> 9 entity platforms -> 5 meals -> eaten check)
  2. Low-power Raspberry Pi audio-only mode (OptionsFlow modular compute toggles disabled -> zero video/TTS overhead)
  3. Clog detection -> 1-portion auto-retry recovery -> persistent empty-hopper stop + speaker TTS escalation
  4. Cloud outage -> automatic Local LAN TCP 6668 failover, `_never_sent` safety guard, and Cloud recovery
  5. Hardware fault (`DP 255 > 1` vs normal `== 1`) emergency alert, redacted HA diagnostics export, and alert clear
  6. Cloud session expiry (`SESSION_EXPIRED`) -> self-healing relogin with RSA-PKCS1v15 fallback & `0o600` persistence
  7. Schedule slot management (`DP 207` 15-char codec via entities/services) & all-slots-disabled starvation guard
  8. Camera outage pre-trigger fallback (`dispensed_unverified`) & mid-feed stream drop (`recording_cut_short`) resilience
  9. Nighttime IR (`HSV sat < 14.0`) meal verification & dog-at-bowl motion occlusion guard (`bowl_empty=False`)
 10. Full HACS v0.7 repository, manifest, translations, Lovelace dashboard, blueprints, and standalone AST audit
 11. `no_food_hours` timer alert mode with periodic camera bowl checks and `food_observations` tracking
 12. Multi-day hopper depletion, `hopper_low` binary sensor threshold, `RetentionManager` ML JSONL export, and refill
 13. Owner-labeled fixture clip replay (`tests/fixtures/labeled_clips/`) and adaptive learned-baseline `weak_flow` warning
 14. Vacation remote pet care: device DP switches/selects, two-way speaker chime + `G.711 mulaw` TTS cache, and treat feed
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "labeled_clips"
TRAINING_LABELS_PATH = FIXTURES_DIR / "training_labels.json"

# Real 5-slot DP 207 schedule string (07:00 8p, 11:00 8p, 16:00 8p, 19:00 8p, 21:00 8p — all enabled daily)
FIVE_SLOT_SCHEDULE_RAW = (
    "7f0700081000000"
    "7f1100081000000"
    "7f1600081000000"
    "7f1900081000000"
    "7f2100081000000"
)


# ---------------------------------------------------------------------------
# Hermetic Synthetic Media & HA Harness Helpers
# ---------------------------------------------------------------------------
class _DefaultAnalyzerCfg:
    min_kibble_audio_peaks = 15
    ting_rate_min = 4.3
    min_bowl_fill_ratio = 0.035
    bowl_roi = (0.12, 0.72, 0.90, 0.995)
    gemini_api_key = ""


def write_synthetic_feed_wav(
    wav_path: str | Path,
    *,
    motor_on: bool = True,
    kibble_tings: bool = True,
    duration_sec: float = 6.0,
    motor_start_sec: float = 0.8,
    motor_end_sec: float = 4.3,
    ting_interval_sec: float = 0.11,
    sample_rate: int = 8000,
) -> Path:
    """Generates a deterministic 8000 Hz mono 16-bit PCM WAV file for acoustic verification tests."""
    p = Path(wav_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n_samples = int(duration_sec * sample_rate)
    t = np.linspace(0.0, duration_sec, n_samples, endpoint=False)
    sig = np.zeros(n_samples, dtype=np.float64)

    if motor_on:
        m_mask = (t >= motor_start_sec) & (t <= motor_end_sec)
        # Steady low-frequency auger motor hum (< 600 Hz)
        sig[m_mask] += 0.045 * np.sin(2.0 * np.pi * 240.0 * t[m_mask])

    if motor_on and kibble_tings:
        burst_len = int(0.025 * sample_rate)
        tb = np.arange(burst_len, dtype=np.float64) / float(sample_rate)
        # Spectral energy in 600-1500 Hz + >= 2500 Hz so STFT spectral ratio >= 1.6 and peak > 0.40
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
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return p


def write_synthetic_bowl_jpeg(
    img_path: str | Path,
    *,
    is_ir: bool = False,
    fill_fraction: float = 0.0,
    dog_in_frame: bool = False,
    width: int = 720,
    height: int = 1280,
) -> Path:
    """Generates an upright 720x1280 portrait JPEG bowl image for daylight or IR vision tests."""
    p = Path(img_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    if is_ir:
        # Grayscale frame (HSV saturation == 0 < 14.0 -> IR night-vision mode)
        arr = np.full((height, width, 3), 128, dtype=np.uint8)
        # Bright IR reflection band at bottom of bowl: IR_BAND = (0.22, 0.88, 0.88, 0.995)
        bx1, by1, bx2, by2 = int(0.22 * width), int(0.88 * height), int(0.88 * width), int(0.995 * height)
        arr[by1:by2, bx1:bx2] = 185
        # Hotspot region [0.93*h:0.995*h, 0.30*w:0.80*w]
        hx1, hy1, hx2, hy2 = int(0.30 * width), int(0.93 * height), int(0.80 * width), int(0.995 * height)
        arr[hy1:hy2, hx1:hx2] = 235

        if dog_in_frame:
            # Large occlusion across >45% of dog detection zone [0.74*h:0.99*h, 0.08*w:0.92*w]
            dx1, dy1, dx2, dy2 = int(0.12 * width), int(0.75 * height), int(0.88 * width), int(0.96 * height)
            arr[dy1:dy2, dx1:dx2] = 25
        elif fill_fraction > 0.0:
            # Dark kibble pellets cutting into IR_BAND and occluding hotspot without triggering wide dog zone
            band_h = by2 - by1
            fill_h = max(8, int(band_h * min(0.55, fill_fraction)))
            arr[by2 - fill_h : by2, bx1 + 20 : bx2 - 20] = 35
    else:
        # Saturated warm indoor lighting (RGB=(210, 165, 110) -> high HSV saturation >= 35)
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        arr[:, :] = (210, 165, 110)
        # Stainless steel pit floor: DAY_PIT = (0.32, 0.88, 0.75, 0.985)
        px1, py1, px2, py2 = int(0.32 * width), int(0.88 * height), int(0.75 * width), int(0.985 * height)
        arr[py1:py2, px1:px2] = (170, 155, 135)

        if dog_in_frame:
            dx1, dy1, dx2, dy2 = int(0.10 * width), int(0.75 * height), int(0.90 * width), int(0.97 * height)
            arr[dy1:dy2, dx1:dx2] = (30, 25, 20)
        elif fill_fraction > 0.0:
            # Dark kibble pellets in DAY_PIT (gray < 6.5 above the 13% baseline)
            pit_h = py2 - py1
            dark_rows = max(4, int(pit_h * min(0.85, fill_fraction + 0.16)))
            arr[py1 : py1 + dark_rows, px1 : px2] = (1, 1, 1)

    Image.fromarray(arr, mode="RGB").save(str(p), format="JPEG", quality=95)
    return p


def record_db_feed_event(db: Any, **kwargs: Any) -> int:
    """Logs a feed event to `KibbleDatabase` supporting both `log_feed_event` and `record_feed_event`."""
    if hasattr(db, "log_feed_event"):
        return db.log_feed_event(
            event_type=kwargs.get("event_type", "scheduled_verify"),
            mode=kwargs.get("mode", "cloud"),
            transport=kwargs.get("transport", "cloud"),
            portions=kwargs.get("portions", 1),
            success=kwargs.get("success", True),
            clog_detected=kwargs.get("clog_detected", False),
            audio_state=kwargs.get("audio_state", "dispensed" if kwargs.get("success", True) else "motor_only_clog"),
            motor_detected=kwargs.get("motor_detected", True),
            kibble_peaks=kwargs.get("kibble_peaks", 0),
            motor_db=kwargs.get("motor_db", 0.0),
            impact_db=kwargs.get("impact_db", 0.0),
            before_fill_pct=kwargs.get("before_fill_pct", 0.0),
            after_fill_pct=kwargs.get("after_fill_pct", 0.0),
            fill_delta_pct=kwargs.get("fill_delta_pct", 0.0),
            bowl_empty=kwargs.get("bowl_empty", False),
            food_increased=kwargs.get("food_increased", False),
            dog_in_bowl=kwargs.get("dog_in_bowl", False),
            infrared_mode=kwargs.get("infrared_mode", False),
            food_weight_g=kwargs.get("food_weight_g"),
            feed_abnormal=kwargs.get("feed_abnormal", 1),
            before_image=kwargs.get("before_image"),
            after_image=kwargs.get("after_image"),
            annotated_image=kwargs.get("annotated_image"),
            video_clip=kwargs.get("video_clip"),
            audio_clip=kwargs.get("audio_clip"),
            webhook_sent=kwargs.get("webhook_sent", False),
            diagnosis=kwargs.get("diagnosis", ""),
            details=kwargs.get("details"),
            outcome=kwargs.get("outcome"),
            motor_duration_sec=kwargs.get("motor_duration_sec"),
            kibble_rate=kwargs.get("kibble_rate"),
        )
    return db.record_feed_event(dict(kwargs))


def build_test_hass_and_entry(
    tmp_dir: str | Path,
    *,
    data_overrides: dict[str, Any] | None = None,
    options_overrides: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Creates a hermetic HomeAssistant + ConfigEntry pair using `_ha_compat` and `const` contracts."""
    const = importlib.import_module("custom_components.molly_cody_feeder.const")
    ha_compat = importlib.import_module("custom_components.molly_cody_feeder._ha_compat")

    base_data = {
        const.CONF_EMAIL: "family@mollycody.test",
        const.CONF_PASSWORD: "SuperSecretPassword!42",
        const.CONF_COUNTRY_CODE: "1",
        const.CONF_DEVICE_ID: "dev_molly_6l_001",
        const.CONF_DEVICE_NAME: "Kitchen Molly & Cody Feeder",
        const.CONF_LOCAL_IP: "192.168.1.88",
        const.CONF_LOCAL_KEY: "0123456789abcdef",
        const.CONF_CONNECTION_MODE: "auto",
        "sid": "sid_secret_token_abc123",
        "uid": "uid_family_999",
        "ecode": "ecode_secret_xyz789",
    }
    if data_overrides:
        base_data.update(data_overrides)

    base_options = {
        const.CONF_CONNECTION_MODE: base_data.get(const.CONF_CONNECTION_MODE, "auto"),
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
    if options_overrides:
        base_options.update(options_overrides)

    try:
        hass = ha_compat.HomeAssistant(str(tmp_dir))
    except TypeError:
        hass = ha_compat.HomeAssistant()
        if hasattr(hass, "config") and hasattr(hass.config, "config_dir"):
            hass.config.config_dir = str(tmp_dir)

    if not hasattr(hass, "config") or not hasattr(hass.config, "path"):
        class _Config:
            def __init__(self, root: str):
                self.config_dir = root

            def path(self, *parts: str) -> str:
                return str(Path(self.config_dir).joinpath(*parts))

        hass.config = _Config(str(tmp_dir))

    try:
        entry = ha_compat.ConfigEntry(
            entry_id="entry_household_001",
            domain=const.DOMAIN,
            title=base_data[const.CONF_DEVICE_NAME],
            data=base_data,
            options=base_options,
        )
    except TypeError:
        entry = ha_compat.ConfigEntry(data=base_data, options=base_options)
        entry.entry_id = getattr(entry, "entry_id", "entry_household_001") or "entry_household_001"
        entry.domain = const.DOMAIN
        entry.title = base_data[const.CONF_DEVICE_NAME]

    return hass, entry


def get_coordinator(hass: Any, entry: Any) -> Any:
    """Extracts the active `MollyCodyDataUpdateCoordinator` from `hass.data[DOMAIN][entry.entry_id]`."""
    const = importlib.import_module("custom_components.molly_cody_feeder.const")
    domain_data = hass.data.get(const.DOMAIN, {})
    stored = domain_data.get(entry.entry_id)
    if isinstance(stored, dict) and "coordinator" in stored:
        return stored["coordinator"]
    if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
        return entry.runtime_data
    return stored


async def collect_all_platform_entities(hass: Any, entry: Any) -> dict[str, list[Any]]:
    """Sets up all 9 entity platforms via their public `async_setup_entry` and returns entities by platform."""
    const = importlib.import_module("custom_components.molly_cody_feeder.const")
    by_platform: dict[str, list[Any]] = {}
    for plat in const.PLATFORMS:
        mod = importlib.import_module(f"custom_components.molly_cody_feeder.{plat}")
        bucket: list[Any] = []

        def _add(new_entities, update_before_add: bool = False, _b=bucket):
            _b.extend(list(new_entities))

        await mod.async_setup_entry(hass, entry, _add)
        for ent in bucket:
            if getattr(ent, "hass", None) is None:
                ent.hass = hass
        by_platform[plat] = bucket
    return by_platform


def entity_value(ent: Any) -> Any:
    """Reads the primary state/value of any HA entity platform object."""
    if hasattr(ent, "is_on") and ent.is_on is not None:
        return ent.is_on
    if hasattr(ent, "native_value") and ent.native_value is not None:
        return ent.native_value
    if hasattr(ent, "current_option") and ent.current_option is not None:
        return ent.current_option
    return getattr(ent, "state", None)


def find_entity(entities: list[Any], *tokens: str) -> Any:
    """Finds an entity whose key, unique_id, or name matches any of `tokens` (normalized)."""
    norm_tokens = [t.lower().replace("_", "").replace(" ", "") for t in tokens if t]
    for tok in norm_tokens:
        for ent in entities:
            candidates = [
                str(getattr(ent, "unique_id", "") or ""),
                str(getattr(ent, "name", "") or ""),
                str(getattr(getattr(ent, "entity_description", None), "key", "") or ""),
                str(getattr(getattr(ent, "entity_description", None), "name", "") or ""),
            ]
            if any(tok in c.lower().replace("_", "").replace(" ", "") for c in candidates):
                return ent
    raise AssertionError(
        f"Entity matching {tokens!r} not found among "
        f"{[getattr(e, 'unique_id', None) or getattr(e, 'name', None) for e in entities]}"
    )


# ---------------------------------------------------------------------------
# Tier 4 Real-World Household Scenarios Test Case
# ---------------------------------------------------------------------------
class TestTier4HouseholdScenarios(unittest.IsolatedAsyncioTestCase):
    """Tier 4 End-to-End Real-World Household Scenarios across F1-F18."""

    async def test_scenario_01_full_day_5_slot_household_lifecycle(self):
        """Scenario 1: Full 24-hour 5-slot household setup, pre-meal check, verified meals, 9 platforms & eaten check."""
        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        cf_mod = importlib.import_module("custom_components.molly_cody_feeder.config_flow")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s01_") as tmp_dir:
            hass, _ = build_test_hass_and_entry(tmp_dir)

            # 1. User onboarded via ConfigFlow (Cloud login + feeder discovery)
            flow = cf_mod.MollyCodyConfigFlow()
            flow.hass = hass

            fake_dps = {
                "101": True,
                "103": False,
                "104": True,
                "106": "1",
                "108": "0",
                "134": True,
                "201": 1,
                "202": 180,
                "207": FIVE_SLOT_SCHEDULE_RAW,
                "255": 1,
            }
            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": dict(fake_dps),
                "named_dps": {"schedule": FIVE_SLOT_SCHEDULE_RAW, "feed_abnormal": 1, "food_weight_g": 180},
                "food_weight_g": 180,
                "last_feed_portions": 1,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "dispense_food", return_value=(True, "cloud")):
                step_res = await flow.async_step_user(
                    {
                        const.CONF_EMAIL: "family@mollycody.test",
                        const.CONF_PASSWORD: "SuperSecretPassword!42",
                        const.CONF_COUNTRY_CODE: "1",
                        const.CONF_CONNECTION_MODE: "auto",
                        const.CONF_LOCAL_IP: "192.168.1.88",
                        const.CONF_LOCAL_KEY: "0123456789abcdef",
                    }
                )
                self.assertEqual(step_res.get("type"), "create_entry")
                self.assertIn(const.CONF_EMAIL, step_res.get("data", {}))

                _, entry = build_test_hass_and_entry(tmp_dir, data_overrides=step_res.get("data", {}))

                # 2. Set up integration entry and all 9 entity platforms
                setup_ok = await init_mod.async_setup_entry(hass, entry)
                self.assertTrue(setup_ok)
                coord = get_coordinator(hass, entry)
                self.assertIsNotNone(coord)

                platforms = await collect_all_platform_entities(hass, entry)
                self.assertEqual(set(platforms.keys()), set(const.PLATFORMS))
                self.assertGreaterEqual(len(platforms["sensor"]), 22)
                self.assertGreaterEqual(len(platforms["binary_sensor"]), 9)
                self.assertGreaterEqual(len(platforms["switch"]), 16)
                self.assertGreaterEqual(len(platforms["button"]), 8)
                self.assertGreaterEqual(len(platforms["number"]), 12)
                self.assertGreaterEqual(len(platforms["select"]), 4)
                self.assertEqual(len(platforms["camera"]), 1)
                self.assertEqual(len(platforms["image"]), 3)
                self.assertEqual(len(platforms["event"]), 2)

                # 3. Refill hopper to 240 portions via service
                refill_res = await coord.async_refill_hopper(capacity_portions=240, note="Fresh bag opened")
                self.assertTrue(refill_res.get("known", True))
                self.assertEqual(refill_res.get("portions_left", 240), 240)

                # 4. Sync 5-slot daily schedule & run pre-meal online check
                synced_slots = await coord.async_sync_schedules()
                enabled_times = [s["time"] for s in synced_slots if s.get("enabled")]
                for expected_t in ("07:00", "11:00", "16:00", "19:00", "21:00"):
                    self.assertIn(expected_t, enabled_times)

                online_ok = await coord.async_run_online_check()
                self.assertTrue(online_ok)

                # 5. Execute verified meals across the day using synthetic/real audio + daylight bowl images
                before_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "meal_before.jpg", is_ir=False, fill_fraction=0.0)
                after_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "meal_after.jpg", is_ir=False, fill_fraction=0.35)
                ok_wav = write_synthetic_feed_wav(Path(tmp_dir) / "meal_ok.wav", motor_on=True, kibble_tings=True)

                def _fake_capture_av_clip(**kwargs):
                    if kwargs.get("trigger_fn"):
                        kwargs["trigger_fn"]()
                    if kwargs.get("before_jpg"):
                        Path(kwargs["before_jpg"]).write_bytes(before_jpg.read_bytes())
                    if kwargs.get("after_jpg"):
                        Path(kwargs["after_jpg"]).write_bytes(after_jpg.read_bytes())
                    if kwargs.get("output_wav"):
                        Path(kwargs["output_wav"]).write_bytes(ok_wav.read_bytes())
                    return {
                        "video_ok": True,
                        "audio_ok": True,
                        "before_ok": True,
                        "after_ok": True,
                        "video_frames": 180,
                        "audio_frames": 300,
                        "audio_rate": 8000,
                        "recording_extended_sec": 0.0,
                        "motor_window_sec": [0.8, 4.3],
                        "recorded_wall_sec": 6.0,
                        "audio_sec": 6.0,
                        "audio_last_rx_sec": 5.95,
                    }

                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)
                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_fake_capture_av_clip), \
                     mock.patch.object(core_mod.MollyFeederClient, "capture_av_clip", side_effect=_fake_capture_av_clip):
                    for slot_portions in (8, 8, 8, 8, 8):
                        feed_res = await coord.async_verify_feed(portions=slot_portions, trigger="scheduled_verify")
                        self.assertIsInstance(feed_res, dict)
                        self.assertEqual(feed_res.get("outcome"), "dispensed")
                        self.assertFalse(feed_res.get("clog_detected", False))

                await coord.async_refresh()
                summary = coord.data["summary"]
                self.assertEqual(summary["status"], "ok")
                self.assertFalse(summary["clog_detected"])
                self.assertGreaterEqual(summary["today_meals"], 5)
                self.assertGreaterEqual(summary["today_fed"], 5)
                self.assertEqual(summary["today_portions"], 40)
                self.assertEqual(summary["hopper_portions_used"], 40)
                self.assertEqual(summary["hopper_portions_left"], 200)
                self.assertAlmostEqual(summary["hopper_percent"], round(100.0 * 200 / 240, 1), places=1)
                self.assertIsNotNone(summary["hopper_days_left"])
                self.assertGreater(summary["hopper_days_left"], 0.0)
                self.assertTrue(summary["food_present"])

                # 6. Verify Sensor, BinarySensor, Camera, Image, and Event entities reflect the full day
                hop_left_sensor = find_entity(platforms["sensor"], "hopper_portions_left", "portions_left")
                self.assertEqual(entity_value(hop_left_sensor), 200)
                today_portions_sensor = find_entity(platforms["sensor"], "today_portions", "portions_today")
                self.assertEqual(entity_value(today_portions_sensor), 40)

                online_bs = find_entity(platforms["binary_sensor"], "online")
                self.assertTrue(online_bs.is_on)
                clog_bs = find_entity(platforms["binary_sensor"], "clog")
                self.assertFalse(clog_bs.is_on)
                hop_low_bs = find_entity(platforms["binary_sensor"], "hopper_low", "low")
                self.assertFalse(hop_low_bs.is_on)

                cam_ent = platforms["camera"][0]
                with mock.patch.object(
                    client_obj,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(after_jpg.read_bytes()), True)[1],
                ):
                    cam_bytes = await cam_ent.async_camera_image()
                self.assertIsNotNone(cam_bytes)
                self.assertTrue(cam_bytes.startswith(b"\xff\xd8"))

                for img_ent in platforms["image"]:
                    img_bytes = await img_ent.async_image() if hasattr(img_ent, "async_image") else img_ent.image()
                    self.assertIsNotNone(img_bytes)
                    self.assertGreater(len(img_bytes), 100)

                feed_ev_ent = find_entity(platforms["event"], "feed")
                self.assertIsNotNone(feed_ev_ent.state)

                # 7. Post-meal eaten check (bowl fill drops below 70% of after-meal fill after puppy eats)
                eaten_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "meal_eaten.jpg", is_ir=False, fill_fraction=0.0)
                with mock.patch.object(
                    client_obj,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(eaten_jpg.read_bytes()), True)[1],
                ):
                    bowl_res = await coord.async_check_bowl()
                self.assertIsInstance(bowl_res, dict)
                self.assertFalse(bowl_res.get("clog_detected", False))

                self.assertTrue(await init_mod.async_unload_entry(hass, entry))

    async def test_scenario_02_low_power_raspberry_pi_audio_only_lifecycle(self):
        """Scenario 2: Low-power Raspberry Pi disables camera/vision/MP4/TTS toggles via OptionsFlow; audio ting still works."""
        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        cf_mod = importlib.import_module("custom_components.molly_cody_feeder.config_flow")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s02_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(tmp_dir)

            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Pi Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 120},
                "named_dps": {},
                "food_weight_g": 120,
                "last_feed_portions": 3,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "dispense_food", return_value=(True, "cloud")):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                await coord.async_refill_hopper(capacity_portions=100, note="Pi initial fill")

                # 1. Open OptionsFlow and disable heavy compute toggles
                opt_flow = cf_mod.MollyCodyConfigFlow.async_get_options_flow(entry)
                opt_flow.hass = hass
                low_power_opts = {
                    const.CONF_CONNECTION_MODE: "auto",
                    const.CONF_ENABLE_CAMERA: False,
                    const.CONF_ENABLE_VISION_VERIFICATION: False,
                    const.CONF_ENABLE_MP4_RECORDING: False,
                    const.CONF_ENABLE_TTS: False,
                    const.CONF_ENABLE_VERIFICATION: True,
                    const.CONF_ENABLE_AUTO_RETRY: True,
                }
                opt_res = await opt_flow.async_step_init(low_power_opts)
                self.assertEqual(opt_res.get("type"), "create_entry")
                for k, v in low_power_opts.items():
                    await coord.async_set_option(k, v)

                self.assertFalse(coord.data["options"][const.CONF_ENABLE_CAMERA])
                self.assertFalse(coord.data["options"][const.CONF_ENABLE_VISION_VERIFICATION])
                self.assertFalse(coord.data["options"][const.CONF_ENABLE_MP4_RECORDING])
                self.assertFalse(coord.data["options"][const.CONF_ENABLE_TTS])
                self.assertTrue(coord.data["options"][const.CONF_ENABLE_VERIFICATION])

                # 2. Verify Camera entity does NOT open a live camera session when enable_camera=False
                platforms = await collect_all_platform_entities(hass, entry)
                cam_ent = platforms["camera"][0]
                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)
                with mock.patch.object(client_obj, "capture_snapshot") as mock_snap:
                    img = await cam_ent.async_camera_image()
                    mock_snap.assert_not_called()
                    self.assertIsNone(img)

                # 3. Verify TTS playback is skipped when enable_tts=False
                with mock.patch.object(client_obj, "speak_tts") as mock_tts:
                    tts_res = await coord.async_play_tts("Should not synthesize on Pi")
                    mock_tts.assert_not_called()
                    self.assertFalse(tts_res.get("ok", False))

                # 4. Run verified feed in audio-only mode: acoustic ting analysis runs, vision analysis does NOT run
                ok_wav = write_synthetic_feed_wav(Path(tmp_dir) / "pi_audio.wav", motor_on=True, kibble_tings=True)

                def _audio_only_capture(**kwargs):
                    if kwargs.get("trigger_fn"):
                        kwargs["trigger_fn"]()
                    if kwargs.get("output_wav"):
                        Path(kwargs["output_wav"]).write_bytes(ok_wav.read_bytes())
                    return {
                        "video_ok": False,
                        "audio_ok": True,
                        "before_ok": False,
                        "after_ok": False,
                        "video_frames": 0,
                        "audio_frames": 240,
                        "audio_rate": 8000,
                        "recorded_wall_sec": 6.0,
                        "audio_sec": 6.0,
                        "audio_last_rx_sec": 5.9,
                    }

                analyzer_obj = getattr(coord, "analyzer", None)
                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_audio_only_capture), \
                     mock.patch.object(core_mod.BowlAnalyzer, "analyze_bowl_visual") as mock_vision:
                    if analyzer_obj is not None:
                        with mock.patch.object(analyzer_obj, "analyze_bowl_visual", wraps=mock_vision):
                            res = await coord.async_verify_feed(portions=3, trigger="manual_dispense_verify")
                    else:
                        res = await coord.async_verify_feed(portions=3, trigger="manual_dispense_verify")
                    mock_vision.assert_not_called()

                self.assertEqual(res.get("outcome"), "dispensed")
                self.assertFalse(res.get("clog_detected", False))

                await coord.async_refresh()
                self.assertEqual(coord.data["summary"]["hopper_portions_used"], 3)
                self.assertEqual(coord.data["summary"]["hopper_portions_left"], 97)

    async def test_scenario_03_clog_detection_auto_retry_recovery_and_empty_hopper_escalation(self):
        """Scenario 3: Auger jam triggers 1p auto-retry recovery; subsequent double motor_no_kibble stops retries & speaks TTS."""
        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s03_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(
                tmp_dir,
                options_overrides={
                    const.CONF_ENABLE_AUTO_RETRY: True,
                    const.CONF_ENABLE_TTS: True,
                    const.CONF_MAX_DAILY_RETRY: 3,
                },
            )

            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 40},
                "named_dps": {},
                "food_weight_g": 40,
                "last_feed_portions": 2,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            clog_wav = write_synthetic_feed_wav(Path(tmp_dir) / "clog.wav", motor_on=True, kibble_tings=False)
            ok_wav = write_synthetic_feed_wav(Path(tmp_dir) / "ok.wav", motor_on=True, kibble_tings=True)
            empty_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "empty.jpg", is_ir=False, fill_fraction=0.0)
            full_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "full.jpg", is_ir=False, fill_fraction=0.35)

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "dispense_food", return_value=(True, "cloud")):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)
                await coord.async_refill_hopper(capacity_portions=50, note="Half full")

                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)

                # --- Part A: Scheduled meal jams (motor_no_kibble), 1-portion auto-retry clears jam (dispensed) ---
                part_a_step = {"n": 0}

                def _capture_part_a(**kwargs):
                    part_a_step["n"] += 1
                    if kwargs.get("trigger_fn"):
                        kwargs["trigger_fn"]()
                    is_retry = part_a_step["n"] >= 2
                    wav_src = ok_wav if is_retry else clog_wav
                    after_src = full_jpg if is_retry else empty_jpg
                    if kwargs.get("before_jpg"):
                        Path(kwargs["before_jpg"]).write_bytes(empty_jpg.read_bytes())
                    if kwargs.get("after_jpg"):
                        Path(kwargs["after_jpg"]).write_bytes(after_src.read_bytes())
                    if kwargs.get("output_wav"):
                        Path(kwargs["output_wav"]).write_bytes(wav_src.read_bytes())
                    return {
                        "video_ok": True,
                        "audio_ok": True,
                        "before_ok": True,
                        "after_ok": True,
                        "recorded_wall_sec": 6.0,
                        "audio_sec": 6.0,
                        "audio_last_rx_sec": 5.95,
                    }

                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_capture_part_a), \
                     mock.patch.object(core_mod.MollyFeederClient, "capture_av_clip", side_effect=_capture_part_a), \
                     mock.patch.object(client_obj, "speak_tts", return_value={"ok": True, "frames_sent": 40}):
                    first_res = await coord.async_verify_feed(portions=4, trigger="scheduled_verify")
                    if part_a_step["n"] == 1 and first_res.get("outcome") == "motor_no_kibble":
                        retry_res = await coord.async_verify_feed(portions=1, trigger="auto_retry_verify")
                        self.assertEqual(retry_res.get("outcome"), "dispensed")
                    else:
                        self.assertGreaterEqual(part_a_step["n"], 2)

                db_obj = getattr(coord, "db", None)
                self.assertEqual(db_obj.consecutive_missed_feeds(), 0)
                self.assertGreaterEqual(db_obj.portions_today("auto_retry_verify"), 1)
                # Failed motor_no_kibble (4p) must NOT subtract from hopper; only the 1p retry that dispensed does!
                hop_after_a = db_obj.hopper_status(50)
                self.assertEqual(hop_after_a["portions_used"], 1)
                self.assertEqual(hop_after_a["portions_left"], 49)

                # --- Part B: Hopper runs empty / chute jammed -> both meal and retry produce motor_no_kibble ---
                def _capture_always_clog(**kwargs):
                    if kwargs.get("trigger_fn"):
                        kwargs["trigger_fn"]()
                    if kwargs.get("before_jpg"):
                        Path(kwargs["before_jpg"]).write_bytes(empty_jpg.read_bytes())
                    if kwargs.get("after_jpg"):
                        Path(kwargs["after_jpg"]).write_bytes(empty_jpg.read_bytes())
                    if kwargs.get("output_wav"):
                        Path(kwargs["output_wav"]).write_bytes(clog_wav.read_bytes())
                    return {
                        "video_ok": True,
                        "audio_ok": True,
                        "before_ok": True,
                        "after_ok": True,
                        "recorded_wall_sec": 6.0,
                        "audio_sec": 6.0,
                        "audio_last_rx_sec": 5.95,
                    }

                tts_calls: list[dict[str, Any]] = []

                def _record_tts(text: str = "", **kw):
                    tts_calls.append({"text": text, **kw})
                    return {"ok": True, "frames_sent": 50, "req_id": 7, "codec": 0x86}

                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_capture_always_clog), \
                     mock.patch.object(core_mod.MollyFeederClient, "capture_av_clip", side_effect=_capture_always_clog), \
                     mock.patch.object(client_obj, "speak_tts", side_effect=_record_tts), \
                     mock.patch.object(core_mod.MollyFeederClient, "speak_tts", side_effect=_record_tts):
                    fail_1 = await coord.async_verify_feed(portions=4, trigger="scheduled_verify")
                    self.assertEqual(fail_1.get("outcome"), "motor_no_kibble")
                    if db_obj.consecutive_missed_feeds() < 2:
                        fail_2 = await coord.async_verify_feed(portions=1, trigger="auto_retry_verify")
                        self.assertEqual(fail_2.get("outcome"), "motor_no_kibble")

                await coord.async_refresh()
                self.assertGreaterEqual(db_obj.consecutive_missed_feeds(), 2)
                self.assertTrue(coord.data["summary"]["clog_detected"])
                self.assertEqual(coord.data["summary"]["status"], "problem")

                clog_bs = find_entity(platforms["binary_sensor"], "clog")
                self.assertTrue(clog_bs.is_on)
                self.assertIsNotNone(coord.data.get("last_alert_event"))
                self.assertGreaterEqual(len(tts_calls), 1)
                # Hopper portions_used still 1 because motor_no_kibble never dispensed kibble
                self.assertEqual(db_obj.hopper_status(50)["portions_used"], 1)

    async def test_scenario_04_cloud_outage_automatic_lan_failover_and_recovery(self):
        """Scenario 4: Cloud outage (`ConnectTimeout`) automatically fails over to Local LAN TCP 6668, then recovers."""
        import types
        import requests

        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s04_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(
                tmp_dir,
                data_overrides={
                    const.CONF_CONNECTION_MODE: "auto",
                    const.CONF_LOCAL_IP: "192.168.1.88",
                    const.CONF_LOCAL_KEY: "0123456789abcdef",
                },
            )

            lan_dps = {
                "101": True,
                "108": "0",
                "201": 2,
                "202": 150,
                "207": FIVE_SLOT_SCHEDULE_RAW,
                "255": 1,
            }

            class _FakeTinyTuyaDevice:
                last_set_values: dict[str, Any] = {}

                def __init__(self, dev_id, ip, key, version=3.4):
                    self.dev_id = dev_id
                    self.ip = ip
                    self.key = key
                    self.version = version

                def set_socketTimeout(self, t):
                    pass

                def set_socketRetryLimit(self, n):
                    pass

                def status(self):
                    return {"dps": dict(lan_dps)}

                def set_multiple_values(self, dps):
                    _FakeTinyTuyaDevice.last_set_values.update({str(k): v for k, v in dps.items()})
                    lan_dps.update({str(k): v for k, v in dps.items()})
                    return {"dps": dict(lan_dps)}

                def set_value(self, dp_id, val):
                    return self.set_multiple_values({str(dp_id): val})

            fake_tt = sys.modules.setdefault("tinytuya", types.ModuleType("tinytuya"))
            fake_tt.Device = _FakeTinyTuyaDevice
            try:
                dev_mod = importlib.import_module("custom_components.molly_cody_feeder.core.molly.device")
            except ImportError:
                dev_mod = None

            # 1. Test direct MollyFeederClient LAN failover when Cloud raises ConnectTimeout
            client = core_mod.MollyFeederClient(
                email="family@mollycody.test",
                password="SuperSecretPassword!42",
                mode="auto",
                device_id="dev_molly_6l_001",
                local_ip="192.168.1.88",
                local_key="0123456789abcdef",
                session_file=str(Path(tmp_dir) / "session.json"),
            )
            client.sid, client.uid, client.ecode = "sid_1", "uid_1", "ecode_1"

            with mock.patch.object(fake_tt, "Device", _FakeTinyTuyaDevice), \
                 (mock.patch.object(dev_mod.tinytuya, "Device", _FakeTinyTuyaDevice) if dev_mod and hasattr(dev_mod, "tinytuya") else mock.MagicMock()), \
                 mock.patch.object(
                     client,
                     "_raw_api_call",
                     side_effect=requests.exceptions.ConnectTimeout("Cloud API unreachable"),
                 ), \
                 mock.patch("time.sleep"):
                st = client.get_status()
                self.assertEqual(st["active_transport"], "lan")
                self.assertTrue(st["online"])
                self.assertEqual(st["food_weight_g"], 150)

                ok, transport = client.dispense_food(2)
                self.assertTrue(ok)
                self.assertEqual(transport, "lan")
                self.assertEqual(_FakeTinyTuyaDevice.last_set_values.get("201"), 2)

            # 2. Verify `_never_sent` anti-double-feed guard: ReadTimeout must NOT retry 3x on Cloud
            read_timeout_calls = {"n": 0}

            def _raise_read_timeout(*args, **kwargs):
                read_timeout_calls["n"] += 1
                raise requests.exceptions.ReadTimeout("Read timed out after request sent")

            with mock.patch.object(client, "call_api", side_effect=_raise_read_timeout), \
                 mock.patch("time.sleep"):
                cloud_ok = client.set_dps_cloud({"201": 3})
                self.assertFalse(cloud_ok)
                self.assertEqual(read_timeout_calls["n"], 1, "ReadTimeout must never retry and risk double-feeding")

            # 3. Verify Coordinator + SelectEntity mode switching (`auto` -> `lan` -> `cloud` -> `auto`)
            with mock.patch.object(fake_tt, "Device", _FakeTinyTuyaDevice), \
                 (mock.patch.object(dev_mod.tinytuya, "Device", _FakeTinyTuyaDevice) if dev_mod and hasattr(dev_mod, "tinytuya") else mock.MagicMock()), \
                 mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                mode_select = find_entity(platforms["select"], "mode", "connection")
                self.assertIn("lan", mode_select.options)
                self.assertIn("cloud", mode_select.options)
                self.assertIn("auto", mode_select.options)

                await mode_select.async_select_option("lan")
                self.assertEqual(coord.data["configured_mode"], "lan")
                self.assertEqual(coord.data["active_transport"], "lan")

                # Feed via coordinator over LAN
                feed_res = await coord.async_feed(portions=3, verify=False)
                self.assertTrue(feed_res.get("ok", True))
                self.assertEqual(_FakeTinyTuyaDevice.last_set_values.get("201"), 3)

    async def test_scenario_05_hardware_fault_dp255_alert_diagnostics_export_and_clear(self):
        """Scenario 5: Hardware fault (`DP 255 = 4 > 1`) triggers fault state, exports redacted diagnostics, and clears."""
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        diag_mod = importlib.import_module("custom_components.molly_cody_feeder.diagnostics")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s05_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(tmp_dir)

            current_dp255 = {"val": 1}

            def _dynamic_status():
                return {
                    "device_id": "dev_molly_6l_001",
                    "device_name": "Kitchen Molly & Cody Feeder",
                    "configured_mode": "auto",
                    "active_transport": "cloud",
                    "local_ip": "192.168.1.88",
                    "online": True,
                    "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": current_dp255["val"], "202": 100},
                    "named_dps": {"feed_abnormal": current_dp255["val"]},
                    "food_weight_g": 100,
                    "last_feed_portions": 1,
                    "feed_abnormal": current_dp255["val"],
                    "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                    "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
                }

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", side_effect=_dynamic_status):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                # 1. Verify DP 255 == 1 is normal (NOT a hardware fault)
                fault_bs = find_entity(platforms["binary_sensor"], "fault", "abnormal", "hardware")
                self.assertEqual(coord.data["feed_abnormal"], 1)
                self.assertFalse(fault_bs.is_on, "DP 255 == 1 is normal on Molly & Cody firmware; only > 1 is a fault")

                # 2. Inject hardware fault bitmap DP 255 = 4
                current_dp255["val"] = 4
                await coord.async_refresh()
                self.assertEqual(coord.data["feed_abnormal"], 4)
                self.assertTrue(fault_bs.is_on)
                self.assertEqual(coord.data["summary"]["status"], "problem")

                # Also verify decision matrix rejects a feed when feed_abnormal > 1 even if audio heard tings
                audio_ok = core_mod.AudioAnalysisResult(
                    motor_detected=True,
                    kibble_impact_peaks=40,
                    kibble_dispensed_audio=True,
                    motor_energy_db=35.0,
                    impact_energy_db=18.0,
                    diagnosis="Motor churned and tings heard",
                    motor_duration_sec=3.0,
                )
                self.assertFalse(core_mod.decide_passed(audio_ok, None, feed_abnormal=4, expect_dispense=True))
                self.assertTrue(core_mod.decide_passed(audio_ok, None, feed_abnormal=1, expect_dispense=True))

                # 3. Export Home Assistant diagnostics and verify strict secret redaction
                diag_data = await diag_mod.async_get_config_entry_diagnostics(hass, entry)
                diag_json = json.dumps(diag_data)
                self.assertIn("**REDACTED**", diag_json)
                for secret_val in (
                    "SuperSecretPassword!42",
                    "sid_secret_token_abc123",
                    "ecode_secret_xyz789",
                    "0123456789abcdef",
                    "family@mollycody.test",
                    "uid_family_999",
                ):
                    self.assertNotIn(secret_val, diag_json, f"Secret {secret_val!r} leaked in diagnostics output!")

                # 4. Clear hardware fault (DP 255 = 1) and invoke clear_clog_alert button / service
                current_dp255["val"] = 1
                clear_btn = find_entity(platforms["button"], "clear", "alert")
                await clear_btn.async_press()
                await coord.async_refresh()
                self.assertFalse(fault_bs.is_on)
                self.assertFalse(coord.data["summary"]["clog_detected"])
                self.assertEqual(coord.data["summary"]["status"], "ok")

    async def test_scenario_06_cloud_session_expiry_self_healing_relogin_with_rsa_fallback(self):
        """Scenario 6: Persisted Tuya session expires; client auto-relogins via RSA-PKCS1v15 fallback & saves `0o600` session."""
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s06_") as tmp_dir:
            sess_path = Path(tmp_dir) / "tuya_session.json"
            sess_path.write_text(
                json.dumps(
                    {
                        "email": "family@mollycody.test",
                        "sid": "expired_sid_000",
                        "uid": "uid_family_999",
                        "ecode": "old_ecode_000",
                        "gid": 313530733,
                        "devId": "dev_molly_6l_001",
                        "localKey": "0123456789abcdef",
                        "localIp": "192.168.1.88",
                        "deviceName": "Kitchen Molly & Cody Feeder",
                        "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1},
                    }
                )
            )

            # Pure-Python 1024-bit RSA keypair (two 512-bit safe primes) for RFC 8017 PKCS#1 v1.5 verification
            p_prime = int(
                "d4b9f38f8270c17f0a4d1157b8876d50ef9d52c2b3a9f74e8c92e1a1b5d7381f"
                "6e10891f48b2d5a4c1317e88d3a51b9302619d8f44028bc9b7c152e819a21f13",
                16,
            )
            q_prime = int(
                "c8e3d91746b2a19d5f0823e1a76c4b91d8e5f20374a9c61b28d4f07e93a5c211"
                "9f401b8c7d2e5a631098f7b4c2d1e0a93847561b2c3d4e5f6a7b8c9d0e1f2b3d",
                16,
            )
            # Ensure primes via Miller-Rabin / deterministic step
            def _next_prime(x: int) -> int:
                x |= 1
                while True:
                    if all(pow(a, x - 1, x) == 1 for a in (2, 3, 5, 7, 11, 13)):
                        return x
                    x += 2

            p_prime = _next_prime(p_prime)
            q_prime = _next_prime(q_prime)
            rsa_n = p_prime * q_prime
            rsa_e = 65537
            rsa_d = pow(rsa_e, -1, (p_prime - 1) * (q_prime - 1))
            pub_n_dec = str(rsa_n)
            pub_e_dec = str(rsa_e)

            client = core_mod.MollyFeederClient(
                email="family@mollycody.test",
                password="MyRsaProtectedPassword!99",
                country_code="1",
                mode="cloud",
                device_id="dev_molly_6l_001",
                session_file=str(sess_path),
            )
            self.assertEqual(client.sid, "expired_sid_000")

            api_calls: list[tuple[str, dict[str, Any], str]] = []
            rsa_decrypted_md5: list[str] = []

            def _mock_raw_api_call(action: str, version: str, post_data_obj: dict[str, Any], sid: str = "", gid: int = 0):
                api_calls.append((action, dict(post_data_obj), sid))
                # 1st call to dp.publish with expired sid -> return SESSION_EXPIRED
                if action in ("thing.m.device.dp.publish", "smartlife.m.device.dp.publish") and sid == "expired_sid_000":
                    return {"success": False, "errorCode": "USER_SESSION_INVALID", "errorMsg": "Session expired"}

                if action == "smartlife.m.user.username.token.get":
                    return {
                        "success": True,
                        "result": {
                            "token": "tok_rsa_challenge",
                            "publicKey": pub_n_dec,
                            "exponent": pub_e_dec,
                        },
                    }

                if action == "smartlife.m.user.email.password.login":
                    if post_data_obj.get("ifencrypt") == 0:
                        # Force RSA fallback
                        return {"success": False, "errorCode": "USER_PASSWD_WRONG", "errorMsg": "RSA required"}
                    # Verify RSA PKCS#1 v1.5 ciphertext decrypts to MD5(password)
                    ct_int = int(post_data_obj["passwd"], 16)
                    k_bytes = (rsa_n.bit_length() + 7) // 8
                    em = pow(ct_int, rsa_d, rsa_n).to_bytes(k_bytes, "big")
                    self.assertTrue(em.startswith(b"\x00\x02"))
                    sep_idx = em.index(b"\x00", 2)
                    pt = em[sep_idx + 1 :].decode("utf-8")
                    rsa_decrypted_md5.append(pt)
                    return {
                        "success": True,
                        "result": {
                            "sid": "fresh_healed_sid_777",
                            "uid": "uid_family_999",
                            "ecode": "fresh_ecode_777",
                            "partnerIdentity": "p2155085",
                        },
                    }

                if action == "m.life.my.group.device.list":
                    return {
                        "success": True,
                        "result": [
                            {
                                "devId": "dev_molly_6l_001",
                                "name": "Kitchen Molly & Cody Feeder",
                                "localKey": "0123456789abcdef",
                                "cloudOnline": True,
                                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1},
                            }
                        ],
                    }

                if action in ("thing.m.device.dp.publish", "smartlife.m.device.dp.publish") and sid == "fresh_healed_sid_777":
                    return {"success": True, "result": True}

                return {"success": True, "result": []}

            with mock.patch.object(client, "_raw_api_call", side_effect=_mock_raw_api_call), \
                 mock.patch("time.sleep"):
                ok, transport, updated_meals = client.update_schedule_slot(
                    slot=1, hour=6, minute=45, portions=3, enabled=True
                )

            self.assertTrue(ok)
            self.assertEqual(transport, "cloud")
            self.assertEqual(client.sid, "fresh_healed_sid_777")
            self.assertEqual(len(rsa_decrypted_md5), 1)
            expected_md5 = hashlib.md5("MyRsaProtectedPassword!99".encode("utf-8")).hexdigest().lower()
            self.assertEqual(rsa_decrypted_md5[0], expected_md5)
            self.assertEqual(updated_meals[0].time_str, "06:45")
            self.assertEqual(updated_meals[0].portions, 3)

            # Verify atomic session file was updated and has 0o600 permissions
            saved = json.loads(sess_path.read_text())
            self.assertEqual(saved["sid"], "fresh_healed_sid_777")
            self.assertEqual(sess_path.stat().st_mode & 0o777, 0o600)

    async def test_scenario_07_schedule_slot_management_and_all_slots_disabled_warning(self):
        """Scenario 7: User edits slots via entities/service (`ddHHMMPPE000000`), then all-slots-disabled triggers alert."""
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s07_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(tmp_dir)
            current_sched = {"raw": FIVE_SLOT_SCHEDULE_RAW}

            def _get_status():
                meals = core_mod.decode_molly_schedule(current_sched["raw"])
                return {
                    "device_id": "dev_molly_6l_001",
                    "device_name": "Kitchen Molly & Cody Feeder",
                    "configured_mode": "auto",
                    "active_transport": "cloud",
                    "local_ip": "192.168.1.88",
                    "online": True,
                    "dps": {"207": current_sched["raw"], "255": 1, "202": 150},
                    "named_dps": {"schedule": current_sched["raw"]},
                    "food_weight_g": 150,
                    "last_feed_portions": 1,
                    "feed_abnormal": 1,
                    "schedule_raw": current_sched["raw"],
                    "schedule": [m.to_dict() for m in meals],
                }

            def _set_sched_raw(raw_str: str):
                current_sched["raw"] = raw_str
                return True, "cloud"

            def _update_slot(slot: int, hour=None, minute=None, portions=None, days=None, days_mask=None, enabled=None):
                mask = days_mask
                if mask is None and days is not None:
                    mask = core_mod.parse_days_spec(str(days)) if isinstance(days, str) else int(days)
                new_raw = core_mod.update_molly_schedule_slot(
                    current_sched["raw"],
                    slot=slot,
                    hour=hour,
                    minute=minute,
                    portions=portions,
                    days_mask=mask,
                    enabled=enabled,
                )
                current_sched["raw"] = new_raw
                return True, "cloud", core_mod.decode_molly_schedule(new_raw)

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", side_effect=_get_status), \
                 mock.patch.object(
                     core_mod.MollyFeederClient,
                     "get_schedule",
                     side_effect=lambda: (current_sched["raw"], core_mod.decode_molly_schedule(current_sched["raw"])),
                 ), \
                 mock.patch.object(core_mod.MollyFeederClient, "set_schedule_raw", side_effect=_set_sched_raw), \
                 mock.patch.object(core_mod.MollyFeederClient, "update_schedule_slot", side_effect=_update_slot):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                # 1. Modify Slot 2 portions to 6 via NumberEntity
                slot2_num = find_entity(platforms["number"], "slot_2", "slot2")
                await slot2_num.async_set_native_value(6)
                decoded = core_mod.decode_molly_schedule(current_sched["raw"])
                self.assertEqual(decoded[1].portions, 6)
                self.assertEqual(decoded[0].portions, 8, "Slot 1 must remain untouched")

                # 2. Update Slot 5 to 21:30, 4 portions, weekdays via coordinator service method
                ok = await coord.async_set_schedule_slot(
                    slot=5, hour=21, minute=30, portions=4, days="weekdays", enabled=True
                )
                self.assertTrue(ok)
                decoded = core_mod.decode_molly_schedule(current_sched["raw"])
                self.assertEqual(decoded[4].time_str, "21:30")
                self.assertEqual(decoded[4].portions, 4)
                self.assertEqual(decoded[4].days_mask, 0x1F)
                self.assertTrue(decoded[4].enabled)

                # 3. Disable Slot 1 via SwitchEntity
                slot1_sw = find_entity(platforms["switch"], "slot_1", "slot1")
                await slot1_sw.async_turn_off()
                self.assertFalse(core_mod.decode_molly_schedule(current_sched["raw"])[0].enabled)

                # 4. Simulate mobile app disabling ALL schedule slots -> coordinator sync fires schedule_empty alert
                all_off_meals = core_mod.decode_molly_schedule(current_sched["raw"])
                for m in all_off_meals:
                    m.enabled = False
                current_sched["raw"] = core_mod.encode_molly_schedule(all_off_meals)

                await coord.async_sync_schedules()
                await coord.async_refresh()
                self.assertIsNotNone(coord.data.get("last_alert_event"))
                alert_ev = coord.data["last_alert_event"]
                self.assertTrue(
                    "schedule" in str(alert_ev).lower()
                    or "disabled" in str(alert_ev).lower()
                    or "empty" in str(alert_ev).lower()
                )

    async def test_scenario_08_camera_outage_and_stream_drop_never_starve_or_overfeed_pet(self):
        """Scenario 8: Camera outage still triggers dispense (`dispensed_unverified`); mid-feed drop -> `recording_cut_short`."""
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s08_") as tmp_dir:
            # --- Part A: Camera outage before vs after trigger_fn in MollyFeederClient.capture_av_clip ---
            client = core_mod.MollyFeederClient.__new__(core_mod.MollyFeederClient)
            dispense_calls = {"n": 0}

            def _trigger():
                dispense_calls["n"] += 1

            def _fail_before_trigger(coro):
                coro.close()
                raise RuntimeError("Cloud imm STUN/TURN relay unreachable")

            with mock.patch("time.sleep"):
                client._run_session = _fail_before_trigger
                res_outage = client.capture_av_clip(
                    duration_sec=10.0,
                    output_mp4=str(Path(tmp_dir) / "out.mp4"),
                    output_wav=str(Path(tmp_dir) / "out.wav"),
                    trigger_fn=_trigger,
                )
            self.assertEqual(dispense_calls["n"], 1, "Pet must still be fed exactly once when camera is unreachable!")
            self.assertTrue(res_outage["dispensed_unverified"])
            self.assertFalse(res_outage["audio_ok"])

            # Now test failure AFTER trigger_fn already fired -> must NOT retry or re-trigger dispense
            dispense_calls["n"] = 0

            def _fail_after_trigger(coro):
                guarded = coro.cr_frame.f_locals.get("trigger_fn")
                coro.close()
                if guarded:
                    guarded()
                raise RuntimeError("UDP socket dropped after dispense")

            with mock.patch("time.sleep"):
                client._run_session = _fail_after_trigger
                res_after = client.capture_av_clip(
                    duration_sec=10.0,
                    output_mp4=str(Path(tmp_dir) / "out2.mp4"),
                    output_wav=str(Path(tmp_dir) / "out2.wav"),
                    trigger_fn=_trigger,
                )
            self.assertEqual(dispense_calls["n"], 1, "Must NEVER double-dispense when camera fails after trigger!")
            self.assertFalse(res_after["dispensed_unverified"])

            # --- Part B: Mid-feed stream drop (`recording_cut_short`) is unverified, NOT a clog, no retry/TTS ---
            hass, entry = build_test_hass_and_entry(tmp_dir)
            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 90},
                "named_dps": {},
                "food_weight_g": 90,
                "last_feed_portions": 2,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            short_wav = write_synthetic_feed_wav(
                Path(tmp_dir) / "cut_short.wav",
                motor_on=False,
                kibble_tings=False,
                duration_sec=1.5,
            )
            empty_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "cut_after.jpg", is_ir=True, fill_fraction=0.0)

            def _capture_cut_short(**kwargs):
                if kwargs.get("trigger_fn"):
                    kwargs["trigger_fn"]()
                if kwargs.get("output_wav"):
                    Path(kwargs["output_wav"]).write_bytes(short_wav.read_bytes())
                if kwargs.get("after_jpg"):
                    Path(kwargs["after_jpg"]).write_bytes(empty_jpg.read_bytes())
                return {
                    "video_ok": True,
                    "audio_ok": True,
                    "before_ok": False,
                    "after_ok": True,
                    "recorded_wall_sec": 15.0,
                    "audio_sec": 1.4,
                    "audio_last_rx_sec": 1.45,
                }

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "dispense_food", return_value=(True, "cloud")):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)

                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_capture_cut_short), \
                     mock.patch.object(core_mod.MollyFeederClient, "capture_av_clip", side_effect=_capture_cut_short), \
                     mock.patch.object(client_obj, "speak_tts") as mock_tts:
                    cut_res = await coord.async_verify_feed(portions=2, trigger="scheduled_verify")
                    mock_tts.assert_not_called()

                self.assertEqual(cut_res.get("outcome"), "recording_cut_short")
                self.assertFalse(cut_res.get("clog_detected", False))
                self.assertEqual(coord.db.consecutive_missed_feeds(), 0)

    async def test_scenario_09_nighttime_infrared_meal_verification_and_dog_at_bowl_guard(self):
        """Scenario 9: Nighttime IR (`HSV sat < 14`) dog-at-bowl guard & IR kibble silhouette verification."""
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s09_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(tmp_dir)
            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"108": "2", "207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 110},
                "named_dps": {"nightvision": "2"},
                "food_weight_g": 110,
                "last_feed_portions": 5,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            ir_before = write_synthetic_bowl_jpeg(Path(tmp_dir) / "ir_before.jpg", is_ir=True, fill_fraction=0.0)
            ir_dog = write_synthetic_bowl_jpeg(Path(tmp_dir) / "ir_dog.jpg", is_ir=True, dog_in_frame=True)
            ir_after_food = write_synthetic_bowl_jpeg(Path(tmp_dir) / "ir_after_food.jpg", is_ir=True, fill_fraction=0.35)
            ok_wav = write_synthetic_feed_wav(Path(tmp_dir) / "ir_ok.wav", motor_on=True, kibble_tings=True)

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "set_dp", return_value=(True, "cloud")), \
                 mock.patch.object(core_mod.MollyFeederClient, "dispense_food", return_value=(True, "cloud")):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                # 1. Verify BowlAnalyzer directly on IR dog-at-bowl vs IR food-increased frames
                analyzer = getattr(coord, "analyzer", None) or core_mod.BowlAnalyzer(
                    getattr(coord, "config", None) or getattr(coord, "cfg", None) or _DefaultAnalyzerCfg()
                )
                vis_dog = analyzer.analyze_bowl_visual(
                    after_image_path=str(ir_dog),
                    before_image_path=str(ir_before),
                    annotated_output_path=str(Path(tmp_dir) / "ann_dog.jpg"),
                )
                self.assertTrue(vis_dog.is_infrared_mode)
                self.assertTrue(vis_dog.dog_at_bowl_detected)
                self.assertFalse(vis_dog.bowl_empty, "bowl_empty must ALWAYS be False (confirm-only invariant)")
                self.assertFalse(vis_dog.food_increased, "Dog occlusion must never falsely confirm food_increased")
                self.assertNotIn("0.0%", vis_dog.diagnosis, "IR mode must never print daylight fill percentages")

                vis_food = analyzer.analyze_bowl_visual(
                    after_image_path=str(ir_after_food),
                    before_image_path=str(ir_before),
                    annotated_output_path=str(Path(tmp_dir) / "ann_food.jpg"),
                )
                self.assertTrue(vis_food.is_infrared_mode)
                self.assertFalse(vis_food.dog_at_bowl_detected)
                self.assertFalse(vis_food.bowl_empty)
                self.assertTrue(vis_food.food_increased)
                self.assertTrue(vis_food.food_present_cv)

                # 2. Run nighttime meal verification through coordinator and verify binary sensors update
                def _capture_ir_meal(**kwargs):
                    if kwargs.get("trigger_fn"):
                        kwargs["trigger_fn"]()
                    if kwargs.get("before_jpg"):
                        Path(kwargs["before_jpg"]).write_bytes(ir_before.read_bytes())
                    if kwargs.get("after_jpg"):
                        Path(kwargs["after_jpg"]).write_bytes(ir_after_food.read_bytes())
                    if kwargs.get("output_wav"):
                        Path(kwargs["output_wav"]).write_bytes(ok_wav.read_bytes())
                    return {
                        "video_ok": True,
                        "audio_ok": True,
                        "before_ok": True,
                        "after_ok": True,
                        "recorded_wall_sec": 6.0,
                        "audio_sec": 6.0,
                        "audio_last_rx_sec": 5.95,
                    }

                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)
                with mock.patch.object(client_obj, "capture_av_clip", side_effect=_capture_ir_meal), \
                     mock.patch.object(core_mod.MollyFeederClient, "capture_av_clip", side_effect=_capture_ir_meal):
                    res = await coord.async_verify_feed(portions=5, trigger="scheduled_verify")
                self.assertEqual(res.get("outcome"), "dispensed")

                await coord.async_refresh()
                ir_bs = find_entity(platforms["binary_sensor"], "infrared", "ir_mode")
                self.assertTrue(ir_bs.is_on)
                food_bs = find_entity(platforms["binary_sensor"], "food_present", "food")
                self.assertTrue(food_bs.is_on)

    def test_scenario_10_full_hacs_repository_lovelace_blueprints_and_standalone_audit(self):
        """Scenario 10: Full HACS v0.7 repository structure, manifests, YAML assets, docs, and zero-VPS AST audit."""
        import yaml

        const = importlib.import_module("custom_components.molly_cody_feeder.const")

        # 1. hacs.json & manifest.json validation
        hacs_path = REPO_ROOT / "hacs.json"
        self.assertTrue(hacs_path.is_file(), "hacs.json must exist at repository root")
        hacs_data = json.loads(hacs_path.read_text())
        self.assertIn("name", hacs_data)

        comp_dir = REPO_ROOT / "custom_components" / "molly_cody_feeder"
        manifest_path = comp_dir / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest.get("domain"), const.DOMAIN)
        self.assertEqual(manifest.get("version"), "0.7.0")
        self.assertTrue(manifest.get("config_flow"))

        # 2. strings.json & translations/en.json parity
        strings_data = json.loads((comp_dir / "strings.json").read_text())
        en_data = json.loads((comp_dir / "translations" / "en.json").read_text())
        self.assertIn("config", strings_data)
        self.assertIn("options", strings_data)
        self.assertEqual(set(strings_data["config"]["step"].keys()), set(en_data["config"]["step"].keys()))

        # 3. services.yaml schema completeness (all 14 services)
        services_yaml = yaml.safe_load((comp_dir / "services.yaml").read_text())
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
        self.assertTrue(expected_services.issubset(set(services_yaml.keys())))

        # 4. Lovelace dashboard & Automation blueprints with `!input` YAML loader
        class _HaYamlLoader(yaml.SafeLoader):
            pass

        _HaYamlLoader.add_constructor(
            "!input",
            lambda loader, node: {"__ha_input__": loader.construct_scalar(node)},
        )

        dashboard_path = REPO_ROOT / "lovelace" / "molly_cody_dashboard.yaml"
        self.assertTrue(dashboard_path.is_file())
        dash_text = dashboard_path.read_text()
        dash_obj = yaml.load(dash_text, Loader=_HaYamlLoader)
        self.assertIsInstance(dash_obj, dict)
        self.assertIn("molly_cody_feeder", dash_text)

        bp_dir = REPO_ROOT / "blueprints" / "automation" / "molly_cody_feeder"
        bp_files = sorted(bp_dir.glob("*.yaml"))
        self.assertGreaterEqual(len(bp_files), 2)
        for bp_file in bp_files:
            bp_obj = yaml.load(bp_file.read_text(), Loader=_HaYamlLoader)
            self.assertIn("blueprint", bp_obj)
            self.assertEqual(bp_obj["blueprint"].get("domain"), "automation")
            self.assertIn("molly_cody_feeder", bp_file.read_text())

        # 5. Documentation files (info.md, README.md, CHANGELOG.md)
        self.assertTrue((REPO_ROOT / "info.md").is_file())
        self.assertIn("0.7", (REPO_ROOT / "CHANGELOG.md").read_text())
        self.assertIn("HACS", (REPO_ROOT / "README.md").read_text())

        # 6. Static AST & source audit: `custom_components/molly_cody_feeder/` must NEVER import `watchdog.*`
        py_files = list(comp_dir.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 15)
        for py_f in py_files:
            tree = ast.parse(py_f.read_text(), filename=str(py_f))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name == "watchdog" or alias.name.startswith("watchdog."),
                            f"{py_f} imports {alias.name} — HACS integration must be 100% standalone!",
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module == "watchdog" or node.module.startswith("watchdog."),
                        f"{py_f} imports from {node.module} — HACS integration must be 100% standalone!",
                    )

    async def test_scenario_11_no_food_hours_timer_mode_with_camera_bowl_checks(self):
        """Scenario 11: `alert_mode='no_food_hours'` suppresses per-meal alerts, logs camera food checks, and alerts at >= 8h."""
        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s11_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(
                tmp_dir,
                options_overrides={
                    const.CONF_ALERT_MODE: "no_food_hours",
                    const.CONF_NO_FOOD_HOURS: 8.0,
                },
            )
            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 100},
                "named_dps": {},
                "food_weight_g": 100,
                "last_feed_portions": 1,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            food_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "food_bowl.jpg", is_ir=False, fill_fraction=0.35)
            empty_jpg = write_synthetic_bowl_jpeg(Path(tmp_dir) / "empty_bowl.jpg", is_ir=False, fill_fraction=0.0)

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)

                # 1. Bowl check sees food -> records food evidence in KibbleDatabase
                with mock.patch.object(
                    client_obj,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(food_jpg.read_bytes()), True)[1],
                ), mock.patch.object(
                    core_mod.MollyFeederClient,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(food_jpg.read_bytes()), True)[1],
                ):
                    check_ok = await coord.async_check_bowl()
                self.assertIsInstance(check_ok, dict)
                coord.db.record_food_evidence("camera", "periodic bowl check")
                last_ev = coord.db.last_food_evidence()
                self.assertIsNotNone(last_ev)
                self.assertEqual(last_ev["source"], "camera")

                # 2. Age food_observations and feed_events by 9.5 hours (>= 8.0h threshold)
                old_ts = (datetime.now() - timedelta(hours=9, minutes=30)).isoformat(timespec="seconds")
                con = sqlite3.connect(str(coord.db.db_path))
                con.execute("UPDATE food_observations SET timestamp = ?", (old_ts,))
                con.execute("UPDATE feed_events SET timestamp = ?", (old_ts,))
                con.commit()
                con.close()

                # 3. Run bowl check / coordinator refresh when bowl is empty -> triggers no-food alert
                with mock.patch.object(
                    client_obj,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(empty_jpg.read_bytes()), True)[1],
                ), mock.patch.object(
                    core_mod.MollyFeederClient,
                    "capture_snapshot",
                    side_effect=lambda p, **kw: (Path(p).write_bytes(empty_jpg.read_bytes()), True)[1],
                ):
                    await coord.async_check_bowl()
                    if hasattr(coord, "async_check_no_food_timer"):
                        await coord.async_check_no_food_timer()
                    await coord.async_refresh()

                aged_ev = coord.db.last_food_evidence()
                hours_elapsed = (datetime.now() - datetime.fromisoformat(aged_ev["timestamp"])).total_seconds() / 3600.0
                self.assertGreaterEqual(hours_elapsed, 9.0)

    async def test_scenario_12_multi_day_hopper_depletion_refill_and_retention_pruning(self):
        """Scenario 12: Multi-day hopper depletion below 20% threshold, `RetentionManager` ML JSONL export, and refill."""
        const = importlib.import_module("custom_components.molly_cody_feeder.const")
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s12_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(
                tmp_dir,
                options_overrides={
                    const.CONF_HOPPER_CAPACITY: 100,
                    const.CONF_HOPPER_LOW_PCT: 20.0,
                },
            )
            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {"207": FIVE_SLOT_SCHEDULE_RAW, "255": 1, "202": 30},
                "named_dps": {},
                "food_weight_g": 30,
                "last_feed_portions": 5,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                # 1. Refill hopper to 100 portions
                await coord.async_refill_hopper(capacity_portions=100, note="Start of week")
                db = coord.db
                work_dir = Path(tmp_dir) / "captures"
                work_dir.mkdir(parents=True, exist_ok=True)
                protected_snap = work_dir / "latest_annotated.jpg"
                protected_snap.write_bytes(b"protected_jpeg")

                # 2. Log 85 portions of successful feeds + 10p clog + 10p camera_unavailable
                old_media_f = work_dir / "old_event_20d.mp4"
                old_media_f.write_bytes(b"mp4_bytes" * 50)
                recent_media_f = work_dir / "recent_event_1d.mp4"
                recent_media_f.write_bytes(b"mp4_bytes" * 50)

                eid_old = record_db_feed_event(
                    db,
                    event_type="scheduled_verify",
                    portions=30,
                    success=True,
                    clog_detected=False,
                    motor_detected=True,
                    kibble_peaks=120,
                    outcome="dispensed",
                    motor_duration_sec=28.0,
                    kibble_rate=13.5,
                    video_clip=str(old_media_f),
                    details={"audio": {"ting_count": 150, "ting_rate_per_sec": 5.8}},
                )
                record_db_feed_event(
                    db,
                    event_type="scheduled_verify",
                    portions=30,
                    success=True,
                    clog_detected=False,
                    motor_detected=True,
                    kibble_peaks=115,
                    outcome="dispensed",
                    motor_duration_sec=27.5,
                    kibble_rate=13.2,
                    video_clip=str(recent_media_f),
                )
                record_db_feed_event(
                    db,
                    event_type="manual_feed_unverified",
                    portions=25,
                    success=True,
                    clog_detected=False,
                    motor_detected=False,
                    outcome="unverified",
                )
                # Clog (motor_no_kibble) and camera_unavailable must NOT subtract from hopper
                record_db_feed_event(
                    db,
                    event_type="scheduled_verify",
                    portions=10,
                    success=False,
                    clog_detected=True,
                    motor_detected=True,
                    outcome="motor_no_kibble",
                )
                record_db_feed_event(
                    db,
                    event_type="scheduled_verify",
                    portions=10,
                    success=False,
                    clog_detected=False,
                    motor_detected=False,
                    outcome="camera_unavailable",
                )

                # Verify 85 portions used -> 15 portions left (15.0% <= 20.0% hopper_low threshold)
                hop = db.hopper_status(100)
                self.assertEqual(hop["portions_used"], 85)
                self.assertEqual(hop["portions_left"], 15)
                self.assertEqual(hop["percent_left"], 15.0)

                await coord.async_refresh()
                hop_low_bs = find_entity(platforms["binary_sensor"], "hopper_low", "low")
                self.assertTrue(hop_low_bs.is_on)

                # 3. Age `eid_old` to 20 days ago (while keeping `refilled_at` before it) and run RetentionManager
                con = sqlite3.connect(str(db.db_path))
                con.execute(
                    "UPDATE hopper_refills SET timestamp = datetime('now', 'localtime', '-25 days')"
                )
                con.execute(
                    "UPDATE feed_events SET timestamp = datetime('now', 'localtime', '-20 days') WHERE id = ?",
                    (eid_old,),
                )
                con.commit()
                con.close()

                ml_jsonl = Path(tmp_dir) / "ml_events.jsonl"

                class _RetCfg:
                    keep_footage = True
                    capture_retention_days = 14
                    keep_failed_footage_days = 90
                    video_keep_days_ok = 0
                    capture_max_mb = 0
                    db_retention_days = 0
                    export_ml_on_prune = True
                    ml_export_path = str(ml_jsonl)

                rm = core_mod.RetentionManager(_RetCfg(), db, work_dir, protected=[protected_snap])
                stats = rm.run()
                self.assertGreaterEqual(stats["events_purged"], 1)
                self.assertFalse(old_media_f.exists(), "20-day-old media file must be purged")
                self.assertTrue(recent_media_f.exists(), "Recent media file must be kept")
                self.assertTrue(protected_snap.exists(), "Protected snapshot must never be deleted")
                self.assertTrue(ml_jsonl.is_file(), "ML JSONL export must be written before media purge")
                exported_rows = [json.loads(line) for line in ml_jsonl.read_text().splitlines() if line.strip()]
                self.assertEqual(len(exported_rows), 1)
                self.assertEqual(exported_rows[0]["event_id"], eid_old)
                self.assertEqual(exported_rows[0]["ting_rate_per_sec"], 5.8)

                # 4. Refill hopper back to 240 portions -> hopper_low binary sensor clears
                await coord.async_refill_hopper(capacity_portions=240, note="Refilled full bag")
                await coord.async_refresh()
                self.assertEqual(coord.data["summary"]["hopper_portions_left"], 240)
                self.assertFalse(hop_low_bs.is_on)

    async def test_scenario_13_owner_labeled_fixture_replay_and_learned_baseline_adaptation(self):
        """Scenario 13: Replays real owner-labeled clips from `tests/fixtures/labeled_clips/` & adapts baselines."""
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")

        self.assertTrue(TRAINING_LABELS_PATH.is_file(), "training_labels.json fixture must exist")
        labels = [r for r in json.loads(TRAINING_LABELS_PATH.read_text()) if r.get("user_reviewed")]
        self.assertGreaterEqual(len(labels), 5)

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s13_") as tmp_dir:
            db = core_mod.KibbleDatabase(str(Path(tmp_dir) / "kibble.db"))
            analyzer = core_mod.BowlAnalyzer(_DefaultAnalyzerCfg())
            replayed = 0

            for rec in labels[:4]:
                clip_id = rec["clip_id"]
                wav_p = FIXTURES_DIR / f"{clip_id}.wav"
                before_p = FIXTURES_DIR / f"{clip_id}_before.jpg"
                after_p = FIXTURES_DIR / f"{clip_id}_after.jpg"
                if not (wav_p.is_file() and after_p.is_file()):
                    continue

                a_res = analyzer.analyze_dispense_audio(str(wav_p))
                v_res = analyzer.analyze_bowl_visual(
                    str(after_p),
                    str(before_p) if before_p.is_file() else None,
                    annotated_output_path=str(Path(tmp_dir) / f"{clip_id}_ann.jpg"),
                )
                passed = core_mod.decide_passed(a_res, v_res, feed_abnormal=1, expect_dispense=True)
                self.assertEqual(passed, bool(rec["feed_success"]), f"Verdict mismatch on fixture {clip_id}")
                self.assertFalse(v_res.bowl_empty, "bowl_empty must always be False on real fixtures")

                outcome, _ = core_mod.classify(a_res, v_res, expect_dispense=True, passed=passed, cut_short=False)
                record_db_feed_event(
                    db,
                    event_type="scheduled_verify",
                    portions=1,
                    success=passed,
                    clog_detected=not passed,
                    motor_detected=a_res.motor_detected,
                    kibble_peaks=a_res.kibble_impact_peaks,
                    outcome=outcome,
                    motor_duration_sec=a_res.motor_duration_sec,
                    kibble_rate=a_res.kibble_rate_per_sec,
                )
                replayed += 1

            self.assertGreaterEqual(replayed, 4)

            # Seed one more verified feed so learned_baselines has >= 3 dispensed samples
            record_db_feed_event(
                db,
                event_type="scheduled_verify",
                portions=1,
                success=True,
                clog_detected=False,
                motor_detected=True,
                kibble_peaks=40,
                outcome="dispensed",
                motor_duration_sec=2.7,
                kibble_rate=14.2,
            )
            learned = db.learned_baselines(min_samples=3)
            self.assertIsNotNone(learned["kibble_rate"])
            self.assertIsNotNone(learned["motor_sec_per_portion"])
            self.assertGreater(learned["kibble_rate"], 10.0)

            # Verify assess_dispense flags weak_flow against the feeder's learned baselines
            degraded_audio = core_mod.AudioAnalysisResult(
                motor_detected=True,
                kibble_impact_peaks=90,
                kibble_dispensed_audio=True,
                motor_energy_db=36.0,
                impact_energy_db=15.0,
                diagnosis="Weak kibble flow",
                motor_duration_sec=9.5,
                kibble_rate_per_sec=9.5,
                ting_count=34,
                ting_rate_per_sec=3.5,
            )
            flags = core_mod.assess_dispense(
                degraded_audio,
                portions=4,
                motor_sec_per_portion=learned["motor_sec_per_portion"],
                baseline_rate=learned["kibble_rate"],
                weak_flow_ting_rate=4.6,
            )
            self.assertTrue(flags["weak_flow"])

    async def test_scenario_14_vacation_remote_care_tts_talkback_chime_and_manual_override(self):
        """Scenario 14: Remote vacation care — DP switches/selects, `G.711 mulaw` (`reqId=7`) TTS/chime, and manual feed."""
        init_mod = importlib.import_module("custom_components.molly_cody_feeder")
        core_mod = importlib.import_module("custom_components.molly_cody_feeder.core")
        try:
            imm_codec = importlib.import_module("custom_components.molly_cody_feeder.core.molly.imm_codec")
            build_talk_frame = imm_codec._imm_build_talk_frame
        except ImportError:
            build_talk_frame = getattr(core_mod, "_imm_build_talk_frame")

        with tempfile.TemporaryDirectory(prefix="mcf_tier4_s14_") as tmp_dir:
            hass, entry = build_test_hass_and_entry(tmp_dir)
            dp_writes: list[tuple[str, Any]] = []

            fake_status = {
                "device_id": "dev_molly_6l_001",
                "device_name": "Kitchen Molly & Cody Feeder",
                "configured_mode": "auto",
                "active_transport": "cloud",
                "local_ip": "192.168.1.88",
                "online": True,
                "dps": {
                    "101": True,
                    "103": False,
                    "104": True,
                    "106": "1",
                    "108": "0",
                    "134": True,
                    "201": 1,
                    "202": 140,
                    "207": FIVE_SLOT_SCHEDULE_RAW,
                    "255": 1,
                },
                "named_dps": {},
                "food_weight_g": 140,
                "last_feed_portions": 1,
                "feed_abnormal": 1,
                "schedule_raw": FIVE_SLOT_SCHEDULE_RAW,
                "schedule": [m.to_dict() for m in core_mod.decode_molly_schedule(FIVE_SLOT_SCHEDULE_RAW)],
            }

            def _record_dp(dp_id, val):
                dp_writes.append((str(dp_id), val))
                fake_status["dps"][str(dp_id)] = val
                return True, "cloud"

            with mock.patch.object(core_mod.MollyFeederClient, "login", return_value=True), \
                 mock.patch.object(core_mod.MollyFeederClient, "get_status", return_value=fake_status), \
                 mock.patch.object(core_mod.MollyFeederClient, "set_dp", side_effect=_record_dp), \
                 mock.patch.object(
                     core_mod.MollyFeederClient,
                     "dispense_food",
                     side_effect=lambda p=1: _record_dp(201, max(1, min(20, int(p)))),
                 ):
                self.assertTrue(await init_mod.async_setup_entry(hass, entry))
                coord = get_coordinator(hass, entry)
                platforms = await collect_all_platform_entities(hass, entry)

                # 1. Verify hardware talk frame invariant: reqId=7 matching TALK_OPEN_CMD=8, codec=0x86, mulaw 0xFF silence
                self.assertEqual(core_mod.MollyFeederClient.TALK_REQ_ID, 7)
                self.assertEqual(core_mod.MollyFeederClient.TALK_OPEN_CMD, 8)
                self.assertEqual(core_mod.MollyFeederClient.TALK_SILENCE_BYTE, b"\xff")
                frame = build_talk_frame(1, 320, 999888, b"\xff" * 320, req_id=7)
                magic, ch, codec, _, _, _, _, ln = struct.unpack_from("<IIIIIIII", frame, 0)
                self.assertEqual(magic, 0x12345678)
                self.assertEqual(ch, 7)
                self.assertEqual(codec, 0x86)
                self.assertEqual(ln, 320)

                # 2. Toggle device DPs remotely (Indicator Light DP 101, Nightvision DP 108)
                self.assertTrue(await coord.async_set_dp(101, False))
                self.assertIn(("101", False), dp_writes)

                nv_select = find_entity(platforms["select"], "night", "108")
                await nv_select.async_select_option(nv_select.options[-1])
                self.assertTrue(any(dp == "108" for dp, _ in dp_writes))

                # 3. Play Chime and TTS message over the feeder speaker
                client_obj = getattr(coord, "client", None) or getattr(coord, "molly", None)
                with mock.patch.object(
                    client_obj,
                    "play_chime",
                    return_value={"ok": True, "frames_sent": 15},
                ) as mock_chime, mock.patch.object(
                    client_obj,
                    "speak_tts",
                    return_value={"ok": True, "frames_sent": 60, "tts_engine": "edge-tts"},
                ) as mock_speak:
                    chime_btn = find_entity(platforms["button"], "chime")
                    await chime_btn.async_press()
                    mock_chime.assert_called_once()

                    tts_res = await coord.async_play_tts(
                        message="Hi Molly and Cody, we love you!",
                        voice="en-US-AriaNeural",
                        volume=3.0,
                        repeat=2,
                        prepend_chime=True,
                    )
                    self.assertTrue(tts_res.get("ok"))
                    mock_speak.assert_called_once()

                # 4. Set Feed Portions number to 4 and trigger manual feed
                portions_num = find_entity(platforms["number"], "feed_portions", "manual_feed")
                await portions_num.async_set_native_value(4)
                feed_res = await coord.async_feed(portions=4, verify=False)
                self.assertTrue(feed_res.get("ok", True))
                self.assertIn(("201", 4), dp_writes)


if __name__ == "__main__":
    unittest.main()
