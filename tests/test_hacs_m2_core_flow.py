"""Unit tests for M2: ConfigFlow, OptionsFlow, Coordinator, Audio-Only HAOS Profile, 14 Services, Diagnostics & Translations."""
from __future__ import annotations

import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import wave

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import custom_components.molly_cody_feeder as init_mod
from custom_components.molly_cody_feeder._ha_compat import ConfigEntry, HomeAssistant
from custom_components.molly_cody_feeder import config_flow as cf_mod
from custom_components.molly_cody_feeder import const
from custom_components.molly_cody_feeder.coordinator import (
    MollyCodyDataUpdateCoordinator,
    _normalize_schedule_slots,
)
from custom_components.molly_cody_feeder.core import (
    BowlAnalyzer,
    MollyFeederClient,
    decode_molly_schedule,
)
from custom_components.molly_cody_feeder import diagnostics as diag_mod

import numpy as np

LIVE_SCHEDULE = (
    "7f0700201000001"
    "7f1100201000001"
    "7f1600201000001"
    "7f1900171000001"
    "7f2100170000001"
)


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


class TestM2CoordinatorServicesAndDiagnostics(unittest.IsolatedAsyncioTestCase):
    """Comprehensive unit tests for config_flow.py, coordinator.py, __init__.py, diagnostics.py, services.yaml, strings.json, en.json."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mcf_unit_m2_")
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_hass_and_entry(self, options_overrides: dict | None = None) -> tuple[HomeAssistant, ConfigEntry]:
        hass = HomeAssistant(str(self.tmp_path))
        opts = {
            const.CONF_CONNECTION_MODE: "auto",
            const.CONF_ENABLE_CAMERA: True,
            const.CONF_ENABLE_TTS: True,
            const.CONF_ENABLE_VERIFICATION: True,
            const.CONF_ENABLE_VISION_VERIFICATION: True,
            const.CONF_ENABLE_MP4_RECORDING: False,
            const.CONF_ENABLE_AUTO_RETRY: True,
            const.CONF_HOPPER_CAPACITY: 240,
            const.CONF_HOPPER_LOW_PCT: 20.0,
        }
        if options_overrides:
            opts.update(options_overrides)
        entry = ConfigEntry(
            entry_id="unit_entry_m2",
            domain=const.DOMAIN,
            title="Unit Test Feeder",
            data={
                const.CONF_EMAIL: "owner@mollycody.test",
                const.CONF_PASSWORD: "UnitTestSecretPassword!1",
                const.CONF_COUNTRY_CODE: "1",
                const.CONF_DEVICE_ID: "dev_unit_m2_001",
                const.CONF_DEVICE_NAME: "Unit Test Feeder",
                const.CONF_LOCAL_IP: "192.168.1.99",
                const.CONF_LOCAL_KEY: "abcdef0123456789",
                const.CONF_CONNECTION_MODE: "auto",
            },
            options=opts,
            unique_id="dev_unit_m2_001",
        )
        return hass, entry

    def _fake_status(self) -> dict:
        return {
            "device_id": "dev_unit_m2_001",
            "device_name": "Unit Test Feeder",
            "configured_mode": "auto",
            "active_transport": "cloud",
            "local_ip": "192.168.1.99",
            "online": True,
            "dps": {
                "101": True,
                "103": False,
                "104": True,
                "106": "1",
                "108": "0",
                "134": True,
                "201": 1,
                "202": 150,
                "207": LIVE_SCHEDULE,
                "255": 1,
            },
            "named_dps": {},
            "food_weight_g": 150,
            "last_feed_portions": 1,
            "feed_abnormal": 1,
            "schedule_raw": LIVE_SCHEDULE,
            "schedule": [m.to_dict() for m in decode_molly_schedule(LIVE_SCHEDULE)],
        }

    def test_normalize_schedule_slots_pads_to_8_slots(self) -> None:
        decoded = decode_molly_schedule(LIVE_SCHEDULE)
        self.assertEqual(len(decoded), 5)
        slots = _normalize_schedule_slots(decoded)
        self.assertEqual(len(slots), 8)
        self.assertTrue(slots[0]["enabled"])
        self.assertFalse(slots[4]["enabled"])
        self.assertFalse(slots[5]["enabled"])
        self.assertFalse(slots[7]["enabled"])
        self.assertEqual([s["slot"] for s in slots], [1, 2, 3, 4, 5, 6, 7, 8])

    def test_compute_preroll_time_midnight_wraparound(self) -> None:
        self.assertEqual(MollyCodyDataUpdateCoordinator.compute_preroll_time(0, 0, 40), (23, 59, 20))
        self.assertEqual(MollyCodyDataUpdateCoordinator.compute_preroll_time("00:00", 40), (23, 59, 20))
        self.assertEqual(MollyCodyDataUpdateCoordinator.compute_preroll_time(7, 0, 40), (6, 59, 20))

    async def test_config_flow_and_options_flow_small_compute_haos_audio_only(self) -> None:
        """Test MollyCodyFeederConfigFlow and MollyCodyFeederOptionsFlow with Small-Compute HAOS Audio-Only profile."""
        hass, _ = self._make_hass_and_entry()
        flow = cf_mod.MollyCodyFeederConfigFlow()
        flow.hass = hass

        with mock.patch.object(MollyFeederClient, "login", return_value=True), \
             mock.patch.object(MollyFeederClient, "get_status", return_value=self._fake_status()):
            res = await flow.async_step_user(
                {
                    const.CONF_EMAIL: "pi_owner@mollycody.test",
                    const.CONF_PASSWORD: "PiPassword123!",
                    const.CONF_COUNTRY_CODE: "1",
                    const.CONF_CONNECTION_MODE: "auto",
                    const.CONF_ENABLE_VERIFICATION: True,
                    const.CONF_ENABLE_VISION_VERIFICATION: False,
                    const.CONF_ENABLE_CAMERA: False,
                    const.CONF_ENABLE_MP4_RECORDING: False,
                }
            )
            self.assertEqual(res["type"], "create_entry")
            data = res["data"]
            self.assertTrue(data[const.CONF_ENABLE_VERIFICATION])
            self.assertFalse(data[const.CONF_ENABLE_VISION_VERIFICATION])
            self.assertFalse(data[const.CONF_ENABLE_CAMERA])
            self.assertFalse(data[const.CONF_ENABLE_MP4_RECORDING])

        # Verify OptionsFlow preserves and updates Small-Compute HAOS Audio-Only profile
        _, entry = self._make_hass_and_entry()
        opt_flow = cf_mod.MollyCodyFeederConfigFlow.async_get_options_flow(entry)
        self.assertIsInstance(opt_flow, cf_mod.MollyCodyFeederOptionsFlow)
        opt_flow.hass = hass

        form_res = await opt_flow.async_step_init(None)
        self.assertEqual(form_res["type"], "form")

        opt_res = await opt_flow.async_step_init(
            {
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
                const.CONF_ENABLE_CAMERA: False,
                const.CONF_ENABLE_MP4_RECORDING: False,
            }
        )
        self.assertEqual(opt_res["type"], "create_entry")
        self.assertTrue(opt_res["data"][const.CONF_ENABLE_VERIFICATION])
        self.assertFalse(opt_res["data"][const.CONF_ENABLE_VISION_VERIFICATION])
        self.assertFalse(opt_res["data"][const.CONF_ENABLE_CAMERA])

    async def test_coordinator_async_verify_feed_audio_only_haos_profile(self) -> None:
        """Verify coordinator.async_verify_feed runs 8 kHz acoustic verification when camera & vision are disabled."""
        hass, entry = self._make_hass_and_entry(
            options_overrides={
                const.CONF_ENABLE_VERIFICATION: True,
                const.CONF_ENABLE_VISION_VERIFICATION: False,
                const.CONF_ENABLE_CAMERA: False,
                const.CONF_ENABLE_MP4_RECORDING: False,
            }
        )
        ok_wav = _write_synthetic_feed_wav(self.tmp_path / "audio_only.wav", motor_on=True, kibble_tings=True)
        captured_kwargs: dict = {}

        def _fake_audio_only_capture(**kwargs):
            captured_kwargs.update(kwargs)
            if callable(kwargs.get("trigger_fn")):
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
                "audio_last_rx_sec": 5.95,
            }

        with mock.patch.object(MollyFeederClient, "login", return_value=True), \
             mock.patch.object(MollyFeederClient, "get_status", return_value=self._fake_status()), \
             mock.patch.object(MollyFeederClient, "dispense_food", return_value=(True, "cloud")), \
             mock.patch.object(MollyFeederClient, "capture_av_clip", side_effect=_fake_audio_only_capture), \
             mock.patch.object(BowlAnalyzer, "analyze_bowl_visual") as mock_vision:
            await init_mod.async_setup_entry(hass, entry)
            coord = hass.data[const.DOMAIN][entry.entry_id]["coordinator"]
            await coord.async_refill_hopper(capacity_portions=100, note="Audio-only HAOS fill")

            res = await coord.async_verify_feed(portions=2, trigger="manual_dispense_verify")
            mock_vision.assert_not_called()
            self.assertIsNone(captured_kwargs.get("before_jpg"))
            self.assertIsNone(captured_kwargs.get("after_jpg"))
            self.assertEqual(captured_kwargs.get("output_mp4"), "")
            self.assertTrue(bool(captured_kwargs.get("output_wav")))
            self.assertTrue(res["ok"])
            self.assertTrue(res["passed"])
            self.assertEqual(res["outcome"], "dispensed")
            self.assertFalse(res["clog_detected"])
            self.assertEqual(coord.data["summary"]["hopper_portions_used"], 2)
            self.assertEqual(coord.data["summary"]["hopper_portions_left"], 98)

            await init_mod.async_unload_entry(hass, entry)

    async def test_setup_entry_registers_14_services_and_unloads_cleanly(self) -> None:
        hass, entry = self._make_hass_and_entry()
        with mock.patch.object(MollyFeederClient, "login", return_value=True), \
             mock.patch.object(MollyFeederClient, "get_status", return_value=self._fake_status()), \
             mock.patch.object(MollyFeederClient, "dispense_food", return_value=(True, "cloud")), \
             mock.patch.object(MollyFeederClient, "set_dp", return_value=(True, "cloud")), \
             mock.patch.object(MollyFeederClient, "set_schedule_raw", return_value=(True, "cloud")):
            ok = await init_mod.async_setup_entry(hass, entry)
            self.assertTrue(ok)
            for svc in const.ALL_SERVICES:
                self.assertTrue(hass.services.has_service(const.DOMAIN, svc), f"Missing service {svc}")

            coord = hass.data[const.DOMAIN][entry.entry_id]["coordinator"]
            self.assertIsInstance(coord, MollyCodyDataUpdateCoordinator)
            self.assertEqual(len(coord.data["schedule"]), 8)
            self.assertGreaterEqual(len(coord.data["summary"]), 35)

            await hass.services.async_call(const.DOMAIN, const.SERVICE_FEED, {"portions": 2, "verify": False})
            self.assertEqual(coord.data["summary"]["today_portions"], 2)

            await hass.services.async_call(
                const.DOMAIN, const.SERVICE_REFILL_HOPPER, {"capacity_portions": 200, "note": "Unit test fill"}
            )
            self.assertEqual(coord.data["summary"]["hopper_capacity_portions"], 200)
            self.assertEqual(coord.data["summary"]["hopper_portions_left"], 200)

            await hass.services.async_call(const.DOMAIN, const.SERVICE_SET_DP, {"dp_id": 101, "value": False})
            self.assertEqual(coord.data["dps"]["101"], False)

            coord.data["summary"]["clog_detected"] = True
            await hass.services.async_call(const.DOMAIN, const.SERVICE_CLEAR_CLOG_ALERT, {})
            self.assertFalse(coord.data["summary"]["clog_detected"])

            unload_ok = await init_mod.async_unload_entry(hass, entry)
            self.assertTrue(unload_ok)
            self.assertNotIn(entry.entry_id, hass.data[const.DOMAIN])

    async def test_diagnostics_redacts_all_sensitive_fields_and_serializes_to_json(self) -> None:
        hass, entry = self._make_hass_and_entry()
        with mock.patch.object(MollyFeederClient, "login", return_value=True), \
             mock.patch.object(MollyFeederClient, "get_status", return_value=self._fake_status()):
            await init_mod.async_setup_entry(hass, entry)
            coord = hass.data[const.DOMAIN][entry.entry_id]["coordinator"]
            coord.client.sid = "secret_sid_unit_test"
            coord.client.ecode = "secret_ecode_unit_test"
            coord.client.uid = "secret_uid_unit_test"

            diag = await diag_mod.async_get_config_entry_diagnostics(hass, entry)
            raw_json = json.dumps(diag)
            for secret in (
                "owner@mollycody.test",
                "UnitTestSecretPassword!1",
                "abcdef0123456789",
                "secret_sid_unit_test",
                "secret_ecode_unit_test",
                "secret_uid_unit_test",
            ):
                self.assertNotIn(secret, raw_json)
            self.assertIn("**REDACTED**", raw_json)
            await init_mod.async_unload_entry(hass, entry)

    def test_services_yaml_and_translations_labels_and_parity(self) -> None:
        comp_dir = Path(init_mod.__file__).resolve().parent
        services_yaml = comp_dir / "services.yaml"
        strings_json = comp_dir / "strings.json"
        en_json = comp_dir / "translations" / "en.json"

        self.assertTrue(services_yaml.is_file())
        svc_text = services_yaml.read_text(encoding="utf-8")
        for svc in const.ALL_SERVICES:
            self.assertIn(f"{svc}:", svc_text)

        s_data = json.loads(strings_json.read_text(encoding="utf-8"))
        e_data = json.loads(en_json.read_text(encoding="utf-8"))
        self.assertEqual(set(s_data["config"]["step"].keys()), set(e_data["config"]["step"].keys()))
        for top_key in ("config", "options", "entity", "services"):
            self.assertIn(top_key, s_data)
            self.assertIn(top_key, e_data)

        expected_ver_label = (
            "Audio Anti-Clog Verification (Lightweight 8 kHz Motor vs. Kibble Audio — Recommended for All HAOS Boxes)"
        )
        expected_vis_label = "Visual Inner-Bowl Analysis (Optional OpenCV/Pillow Camera ROI — Higher Compute)"
        expected_cam_label = "Enable Camera Snapshots & Image Entities (Optional)"

        for doc in (s_data, e_data):
            opt_labels = doc["options"]["step"]["init"]["data"]
            self.assertEqual(opt_labels["enable_verification"], expected_ver_label)
            self.assertEqual(opt_labels["enable_vision_verification"], expected_vis_label)
            self.assertEqual(opt_labels["enable_camera"], expected_cam_label)


if __name__ == "__main__":
    unittest.main()
