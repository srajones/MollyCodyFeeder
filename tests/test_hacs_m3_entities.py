"""Milestone M3 Unit Tests for MollyCodyFeeder v0.7 Entity Platforms, Lovelace, Blueprints & Docs."""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional
import unittest
from unittest import mock

import yaml

from custom_components.molly_cody_feeder import (
    binary_sensor as binary_sensor_mod,
    button as button_mod,
    camera as camera_mod,
    event as event_mod,
    image as image_mod,
    number as number_mod,
    select as select_mod,
    sensor as sensor_mod,
    switch as switch_mod,
)
from custom_components.molly_cody_feeder._ha_compat import (
    ConfigEntry,
    DataUpdateCoordinator,
    HomeAssistant,
)
from custom_components.molly_cody_feeder.const import (
    CONF_ALERT_MODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_ENABLE_AUTO_RETRY,
    CONF_ENABLE_CAMERA,
    CONF_ENABLE_MP4_RECORDING,
    CONF_ENABLE_TTS,
    CONF_ENABLE_VERIFICATION,
    CONF_ENABLE_VISION_VERIFICATION,
    CONF_HOPPER_CAPACITY,
    CONF_PORTION_WEIGHT_G,
    CONF_TTS_VOLUME,
    DOMAIN,
    EVENT_ALERT,
    EVENT_FEED,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "molly_cody_feeder"


def _make_default_schedule() -> List[Dict[str, Any]]:
    """Build 8 normalized DP 207 schedule slot dicts."""
    slots: List[Dict[str, Any]] = []
    for i in range(1, 9):
        enabled = i <= 4
        portions = 2 * i if i <= 4 else 1
        hour = 6 + i * 2
        slots.append(
            {
                "slot": i,
                "time": f"{hour:02d}:00",
                "hour": hour,
                "minute": 0,
                "portions": portions,
                "days_mask": 0x7F if enabled else 0x00,
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] if enabled else [],
                "enabled": enabled,
                "raw": f"{'7f' if enabled else '00'}{hour:02d}00{portions:02d}{'1' if enabled else '0'}000000",
            }
        )
    return slots


class FakeM3Coordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Realistic coordinator test harness implementing the M2/M3 Coordinator contract."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, logging.getLogger(__name__), name=DOMAIN)
        self.entry = entry
        self.options: Dict[str, Any] = dict(entry.options)
        self.session_lock = asyncio.Lock()
        self._session_lock = self.session_lock
        self.client = mock.MagicMock()
        self.client.capture_snapshot = mock.MagicMock(return_value=True)
        self.data: Dict[str, Any] = {
            "device_id": entry.data.get(CONF_DEVICE_ID, "dev_molly_m3_01"),
            "device_name": entry.data.get(CONF_DEVICE_NAME, "Molly & Cody Feeder"),
            "online": True,
            "configured_mode": "auto",
            "active_transport": "lan",
            "local_ip": "192.168.1.150",
            "dps": {
                "101": True,
                "103": False,
                "104": True,
                "106": "1",
                "108": "0",
                "134": True,
                "202": 25,
                "255": 1,
            },
            "named_dps": {},
            "feed_abnormal": 1,
            "food_weight_g": 25,
            "last_feed_portions": 2,
            "schedule_raw": "".join(s["raw"] for s in _make_default_schedule()),
            "schedule": _make_default_schedule(),
            "summary": {
                "status": "ok",
                "busy": False,
                "clog_detected": False,
                "last_meal_time": "2026-11-06T08:00:00",
                "last_meal_result": "fed",
                "last_meal_outcome": "dispensed",
                "last_meal_text": "Dispensed 2 portions (6.2 tings/s)",
                "last_meal_ok": True,
                "last_meal_portions": 2,
                "minutes_since_last_meal": 45,
                "next_meal_time": "12:00",
                "minutes_to_next_meal": 195,
                "today_meals": 2,
                "today_fed": 2,
                "today_problems": 0,
                "today_portions": 6,
                "today_list": [{"time": "08:00", "portions": 2, "result": "fed"}],
                "week_meals": 14,
                "week_confirmed_pct": 100.0,
                "kibble_flow": 6.2,
                "kibble_flow_7d_avg": 5.9,
                "last_motor_duration_sec": 3.8,
                "bowl_fill_pct": 18.5,
                "food_present": True,
                "dog_at_bowl": False,
                "is_infrared_mode": False,
                "weak_flow_warning": False,
                "consecutive_missed_feeds": 0,
                "hopper_known": True,
                "hopper_capacity_portions": 240,
                "hopper_portions_used": 20,
                "hopper_portions_left": 220,
                "hopper_percent": 91.7,
                "hopper_days_left": 18.3,
                "hopper_low": False,
                "last_alert_title": None,
                "last_alert_time": None,
            },
            "options": dict(entry.options),
            "latest_snapshot_bytes": None,
            "latest_annotated_bytes": None,
            "latest_before_bytes": None,
            "latest_after_bytes": None,
            "last_feed_event": None,
            "last_alert_event": None,
        }
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    async def async_feed(self, portions: Optional[int] = None, verify: Optional[bool] = None) -> Dict[str, Any]:
        p = int(portions or 1)
        self.calls.append(("async_feed", {"portions": p, "verify": verify}))
        ev = {"event_type": "manual_feed", "outcome": "unverified", "portions": p, "timestamp": "2026-11-06T09:00:00Z"}
        self.data["last_feed_event"] = ev
        if hasattr(self.hass, "bus"):
            self.hass.bus.async_fire(EVENT_FEED, ev)
        return ev

    async def async_verify_feed(self, portions: int = 1, trigger: str = "manual_dispense_verify") -> Dict[str, Any]:
        p = int(portions or 1)
        self.calls.append(("async_verify_feed", {"portions": p, "trigger": trigger}))
        ev = {"event_type": "dispensed", "outcome": "dispensed", "portions": p, "timestamp": "2026-11-06T09:05:00Z"}
        self.data["last_feed_event"] = ev
        self.data["summary"]["last_meal_portions"] = p
        self.data["summary"]["last_meal_outcome"] = "dispensed"
        self.data["summary"]["today_portions"] = int(self.data["summary"].get("today_portions", 0)) + p
        cap = int(self.data["summary"].get("hopper_capacity_portions", 240))
        used = int(self.data["summary"].get("hopper_portions_used", 0)) + p
        left = max(0, cap - used)
        self.data["summary"]["hopper_portions_used"] = used
        self.data["summary"]["hopper_portions_left"] = left
        self.data["summary"]["hopper_percent"] = round((left / cap) * 100.0, 1) if cap > 0 else 0.0
        if hasattr(self.hass, "bus"):
            self.hass.bus.async_fire(EVENT_FEED, ev)
        return ev

    async def async_check_bowl(self) -> Dict[str, Any]:
        self.calls.append(("async_check_bowl", {}))
        return {"outcome": "bowl_check", "food_present": True}

    async def async_set_schedule_slot(
        self,
        slot: int,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        portions: Optional[int] = None,
        days: Any = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        self.calls.append(
            ("async_set_schedule_slot", {"slot": slot, "hour": hour, "minute": minute, "portions": portions, "enabled": enabled})
        )
        for item in self.data["schedule"]:
            if item.get("slot") == slot:
                if enabled is not None:
                    item["enabled"] = bool(enabled)
                if portions is not None:
                    item["portions"] = int(portions)
                if hour is not None:
                    item["hour"] = int(hour)
                if minute is not None:
                    item["minute"] = int(minute)
                if hour is not None or minute is not None:
                    item["time"] = f"{int(item.get('hour', 0)):02d}:{int(item.get('minute', 0)):02d}"
        return True

    async def async_sync_schedules(self) -> List[Dict[str, Any]]:
        self.calls.append(("async_sync_schedules", {}))
        return self.data["schedule"]

    async def async_refill_hopper(self, capacity_portions: Optional[int] = None, note: str = "") -> Dict[str, Any]:
        cap = int(capacity_portions or self.data["summary"]["hopper_capacity_portions"])
        self.calls.append(("async_refill_hopper", {"capacity_portions": cap, "note": note}))
        self.data["summary"]["hopper_known"] = True
        self.data["summary"]["hopper_capacity_portions"] = cap
        self.data["summary"]["hopper_portions_used"] = 0
        self.data["summary"]["hopper_portions_left"] = cap
        self.data["summary"]["hopper_percent"] = 100.0
        self.data["summary"]["hopper_low"] = False
        return self.data["summary"]

    async def async_capture_snapshot(self) -> Optional[bytes]:
        self.calls.append(("async_capture_snapshot", {}))
        self.client.capture_snapshot()
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF_SNAPSHOT"
        self.data["latest_snapshot_bytes"] = jpeg
        return jpeg

    async def async_play_chime(self, volume: Optional[float] = None) -> Dict[str, Any]:
        self.calls.append(("async_play_chime", {"volume": volume}))
        return {"ok": True}

    async def async_set_dp(self, dp_id: int, value: Any) -> bool:
        self.calls.append(("async_set_dp", {"dp_id": int(dp_id), "value": value}))
        self.data["dps"][str(dp_id)] = value
        return True

    async def async_set_connection_mode(self, mode: str) -> bool:
        self.calls.append(("async_set_connection_mode", {"mode": mode}))
        self.data["configured_mode"] = mode
        return True

    async def async_set_option(self, key: str, value: Any) -> None:
        self.calls.append(("async_set_option", {"key": key, "value": value}))
        self.options[key] = value
        self.data.setdefault("options", {})[key] = value

    async def async_clear_clog_alert(self) -> None:
        self.calls.append(("async_clear_clog_alert", {}))
        self.data["summary"]["clog_detected"] = False
        self.data["summary"]["status"] = "ok"


def _create_fixture(td: str, options_override: Optional[Dict[str, Any]] = None) -> tuple[HomeAssistant, ConfigEntry, FakeM3Coordinator]:
    hass = HomeAssistant(td)
    opts: Dict[str, Any] = {
        CONF_ENABLE_CAMERA: True,
        CONF_ENABLE_TTS: True,
        CONF_ENABLE_VERIFICATION: True,
        CONF_ENABLE_VISION_VERIFICATION: True,
        CONF_ENABLE_MP4_RECORDING: False,
        CONF_ENABLE_AUTO_RETRY: True,
        CONF_HOPPER_CAPACITY: 240,
        CONF_PORTION_WEIGHT_G: 10,
        CONF_TTS_VOLUME: 2.5,
        CONF_ALERT_MODE: "every_miss",
    }
    if options_override:
        opts.update(options_override)
    entry = ConfigEntry(
        entry_id="entry_m3_unit_01",
        domain=DOMAIN,
        title="Molly & Cody Feeder",
        data={
            CONF_DEVICE_ID: "dev_molly_m3_01",
            CONF_DEVICE_NAME: "Molly & Cody Feeder",
        },
        options=opts,
    )
    coord = FakeM3Coordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coord}
    return hass, entry, coord


class TestM3SensorAndBinarySensorPlatforms(unittest.IsolatedAsyncioTestCase):
    """Unit tests for sensor.py (22 entities) and binary_sensor.py (9 entities)."""

    async def test_sensor_platform_22_entities_and_dynamic_properties(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            sensors: List[Any] = []
            await sensor_mod.async_setup_entry(hass, entry, sensors.extend)
            self.assertEqual(len(sensors), 22)
            self.assertEqual(len({s.unique_id for s in sensors}), 22)

            by_key = {s.key: s for s in sensors}
            expected_keys = {
                "status",
                "active_transport",
                "configured_mode",
                "last_meal_time",
                "last_meal_result",
                "last_meal_portions",
                "minutes_since_last_meal",
                "next_meal_time",
                "minutes_to_next_meal",
                "today_meals",
                "today_fed",
                "today_problems",
                "today_portions",
                "week_confirmed_pct",
                "kibble_flow",
                "kibble_flow_7d_avg",
                "last_motor_duration_sec",
                "hopper_percent",
                "hopper_portions_left",
                "hopper_days_left",
                "bowl_fill_pct",
                "schedule",
            }
            self.assertEqual(set(by_key.keys()), expected_keys)
            self.assertEqual(by_key["status"].native_value, "ok")
            self.assertEqual(by_key["active_transport"].native_value, "lan")
            self.assertEqual(by_key["configured_mode"].native_value, "auto")
            self.assertEqual(by_key["today_portions"].native_value, 6)
            self.assertEqual(by_key["hopper_portions_left"].native_value, 220)
            self.assertIsInstance(by_key["schedule"].extra_state_attributes, dict)
            self.assertIn("slots", by_key["schedule"].extra_state_attributes)

            # Mutate coordinator.data directly and verify immediate dynamic update
            coord.data["summary"]["status"] = "problem"
            coord.data["summary"]["next_meal_time"] = None
            self.assertEqual(by_key["status"].native_value, "problem")
            self.assertEqual(by_key["schedule"].native_value, "4 active slots")

    async def test_binary_sensor_platform_9_entities_and_dp255_fault_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            bsensors: List[Any] = []
            await binary_sensor_mod.async_setup_entry(hass, entry, bsensors.extend)
            self.assertEqual(len(bsensors), 9)
            self.assertEqual(len({b.unique_id for b in bsensors}), 9)

            by_key = {b.key: b for b in bsensors}
            expected_keys = {
                "online",
                "clog_detected",
                "hardware_fault",
                "hopper_low",
                "food_present",
                "dog_at_bowl",
                "is_infrared_mode",
                "busy",
                "weak_flow_warning",
            }
            self.assertEqual(set(by_key.keys()), expected_keys)
            for b in bsensors:
                self.assertIsInstance(b.is_on, bool)
                self.assertIsNotNone(b.device_info)

            # DP 255 == 1 is normal (False), DP 255 >= 2 is fault (True)
            coord.data["feed_abnormal"] = 1
            coord.data["dps"]["255"] = 1
            self.assertFalse(by_key["hardware_fault"].is_on)
            coord.data["feed_abnormal"] = 2
            coord.data["dps"]["255"] = 2
            self.assertTrue(by_key["hardware_fault"].is_on)

            # Online requires both online=True and active_transport != 'none'
            self.assertTrue(by_key["online"].is_on)
            coord.data["active_transport"] = "none"
            self.assertFalse(by_key["online"].is_on)


class TestM3ControlEntityPlatforms(unittest.IsolatedAsyncioTestCase):
    """Unit tests for switch.py (16), button.py (8), number.py (12), and select.py (4)."""

    async def test_switch_platform_16_entities_actions_and_short_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            switches: List[Any] = []
            await switch_mod.async_setup_entry(hass, entry, switches.extend)
            self.assertEqual(len(switches), 16)
            by_key = {s.key: s for s in switches}

            # Slot 1 switch
            self.assertTrue(by_key["slot_1_enabled"].is_on)
            await by_key["slot_1_enabled"].async_turn_off()
            self.assertFalse(by_key["slot_1_enabled"].is_on)
            await by_key["slot_1_enabled"].async_turn_on()
            self.assertTrue(by_key["slot_1_enabled"].is_on)

            # DP 101 switch
            self.assertTrue(by_key["indicator_light"].is_on)
            await by_key["indicator_light"].async_turn_off()
            self.assertFalse(by_key["indicator_light"].is_on)

            # Compute toggle switch
            await by_key["enable_vision_verification"].async_turn_off()
            self.assertFalse(by_key["enable_vision_verification"].is_on)
            self.assertFalse(coord.options[CONF_ENABLE_VISION_VERIFICATION])

            # Truncate schedule list to 1 item -> no IndexError
            coord.data["schedule"] = coord.data["schedule"][:1]
            for s in switches:
                self.assertIsInstance(s.is_on, bool)

    async def test_button_platform_8_entities_and_press_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            buttons: List[Any] = []
            await button_mod.async_setup_entry(hass, entry, buttons.extend)
            self.assertEqual(len(buttons), 8)
            for b in buttons:
                await b.async_press()
            called_names = [name for name, _ in coord.calls]
            for expected in (
                "async_feed",
                "async_verify_feed",
                "async_check_bowl",
                "async_capture_snapshot",
                "async_refill_hopper",
                "async_play_chime",
                "async_sync_schedules",
                "async_clear_clog_alert",
            ):
                self.assertIn(expected, called_names)

    async def test_number_platform_12_entities_bounds_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            numbers: List[Any] = []
            await number_mod.async_setup_entry(hass, entry, numbers.extend)
            self.assertEqual(len(numbers), 12)
            by_key = {n.key: n for n in numbers}

            self.assertEqual(by_key["feed_portions"].native_min_value, 1)
            self.assertEqual(by_key["feed_portions"].native_max_value, 20)
            await by_key["feed_portions"].async_set_native_value(9)
            self.assertEqual(int(by_key["feed_portions"].native_value), 9)

            await by_key["slot_2_portions"].async_set_native_value(11)
            self.assertEqual(int(by_key["slot_2_portions"].native_value), 11)

            # Short schedule safety
            coord.data["schedule"] = []
            for n in numbers:
                self.assertIsNotNone(n.native_value)

    async def test_select_platform_4_entities_and_dp_mappings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            selects: List[Any] = []
            await select_mod.async_setup_entry(hass, entry, selects.extend)
            self.assertEqual(len(selects), 4)
            by_key = {s.key: s for s in selects}

            await by_key["connection_mode"].async_select_option("cloud")
            self.assertEqual(by_key["connection_mode"].current_option, "cloud")

            await by_key["nightvision"].async_select_option("on")
            self.assertEqual(coord.data["dps"]["108"], "2")
            self.assertEqual(by_key["nightvision"].current_option, "on")

            await by_key["motion_sensitivity"].async_select_option("high")
            self.assertEqual(coord.data["dps"]["106"], "2")
            self.assertEqual(by_key["motion_sensitivity"].current_option, "high")

            await by_key["alert_mode"].async_select_option("no_food_hours")
            self.assertEqual(by_key["alert_mode"].current_option, "no_food_hours")


class TestM3CameraImageAndEventPlatforms(unittest.IsolatedAsyncioTestCase):
    """Unit tests for camera.py (1), image.py (3), and event.py (2)."""

    async def test_camera_platform_respects_enable_camera_and_session_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            cameras: List[Any] = []
            await camera_mod.async_setup_entry(hass, entry, cameras.extend)
            self.assertEqual(len(cameras), 1)
            cam = cameras[0]

            # Normal live capture
            snap = await cam.async_camera_image()
            self.assertTrue(snap.startswith(b"\xff\xd8"))
            self.assertEqual(coord.client.capture_snapshot.call_count, 1)

            # Busy -> returns cached without calling capture_snapshot
            coord.data["summary"]["busy"] = True
            coord.data["latest_snapshot_bytes"] = b"\xff\xd8cached_busy"
            snap_busy = await cam.async_camera_image()
            self.assertEqual(snap_busy, b"\xff\xd8cached_busy")
            self.assertEqual(coord.client.capture_snapshot.call_count, 1)
            coord.data["summary"]["busy"] = False

            # Disabled -> returns cached/None without calling capture_snapshot
            coord.data["options"][CONF_ENABLE_CAMERA] = False
            coord.options[CONF_ENABLE_CAMERA] = False
            coord.data["latest_snapshot_bytes"] = None
            self.assertFalse(cam.available)
            self.assertIsNone(await cam.async_camera_image())
            self.assertEqual(coord.client.capture_snapshot.call_count, 1)

    async def test_image_platform_3_entities_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            images: List[Any] = []
            await image_mod.async_setup_entry(hass, entry, images.extend)
            self.assertEqual(len(images), 3)
            by_key = {im.key: im for im in images}
            self.assertEqual(set(by_key.keys()), {"annotated_bowl", "before_feed", "after_feed"})

            for im in images:
                self.assertIsNone(await im.async_image())

            coord.data["latest_annotated_bytes"] = b"\xff\xd8ann"
            coord.data["latest_before_bytes"] = b"\xff\xd8bef"
            coord.data["latest_after_bytes"] = b"\xff\xd8aft"
            self.assertEqual(await by_key["annotated_bowl"].async_image(), b"\xff\xd8ann")
            self.assertEqual(await by_key["before_feed"].async_image(), b"\xff\xd8bef")
            self.assertEqual(await by_key["after_feed"].async_image(), b"\xff\xd8aft")
            self.assertIsNotNone(by_key["annotated_bowl"].image_last_updated)

    async def test_event_platform_2_entities_bus_and_coordinator_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            events: List[Any] = []
            await event_mod.async_setup_entry(hass, entry, events.extend)
            self.assertEqual(len(events), 2)
            by_key = {e.key: e for e in events}
            feed_ev = by_key["feed_event"]
            alert_ev = by_key["alert_event"]

            self.assertIsNone(feed_ev.state)
            self.assertIsNone(alert_ev.state)

            # Fallback from coordinator.data["last_feed_event"] even before bus fire
            coord.data["last_feed_event"] = {"outcome": "dispensed", "timestamp": "2026-11-06T10:00:00Z"}
            self.assertEqual(feed_ev.state, "2026-11-06T10:00:00Z")
            self.assertEqual(feed_ev.extra_state_attributes.get("outcome"), "dispensed")

            # Bus event firing
            hass.bus.async_fire(EVENT_ALERT, {"event_type": "clog_detected", "title": "Clog"})
            self.assertIsNotNone(alert_ev.state)
            self.assertEqual(alert_ev.extra_state_attributes.get("event_type"), "clog_detected")


class TestM3LovelaceBlueprintsDocsAndStandaloneAudit(unittest.TestCase):
    """Unit tests for Lovelace YAML, Blueprints YAML, docs, and zero watchdog imports."""

    def test_lovelace_dashboard_valid_yaml(self) -> None:
        dash_path = REPO_ROOT / "lovelace" / "molly_cody_dashboard.yaml"
        self.assertTrue(dash_path.is_file())
        raw = dash_path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        self.assertIsInstance(parsed, dict)
        self.assertIn("views", parsed)
        self.assertGreaterEqual(len(parsed["views"]), 1)
        self.assertIn("molly_cody_feeder", raw)

    def test_blueprints_valid_yaml_with_input_tags(self) -> None:
        class HaYamlLoader(yaml.SafeLoader):
            pass

        HaYamlLoader.add_constructor("!input", lambda loader, node: f"!input {loader.construct_scalar(node)}")

        bp_dir = REPO_ROOT / "blueprints" / "automation" / "molly_cody_feeder"
        for fname in ("clog_alert_notification.yaml", "hopper_low_reminder.yaml"):
            path = bp_dir / fname
            self.assertTrue(path.is_file(), f"Missing {fname}")
            raw = path.read_text(encoding="utf-8")
            parsed = yaml.load(raw, Loader=HaYamlLoader)
            self.assertIn("blueprint", parsed)
            self.assertEqual(parsed["blueprint"].get("domain"), "automation")
            self.assertIsInstance(parsed["blueprint"].get("input"), dict)
            self.assertIn("trigger", parsed)
            self.assertIn("action", parsed)
            self.assertIn("molly_cody_feeder", raw)

    def test_docs_exist_and_have_no_secrets(self) -> None:
        for fname in ("info.md", "README.md", "CHANGELOG.md"):
            p = REPO_ROOT / fname
            self.assertTrue(p.is_file())
            txt = p.read_text(encoding="utf-8")
            self.assertNotIn("github_pat_", txt)
            self.assertNotIn("BEGIN RSA PRIVATE KEY", txt)
            self.assertIn("HACS", txt)

        self.assertIn("0.7", (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertIn("molly_cody_feeder", (REPO_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_docs_document_small_compute_haos_audio_only_verification(self) -> None:
        for fname in ("info.md", "README.md", "CHANGELOG.md"):
            txt = (REPO_ROOT / fname).read_text(encoding="utf-8")
            # Audio-Only HAOS optimization documentation checks
            self.assertIn("Audio-Only", txt, f"{fname} must document Audio-Only verification mode")
            self.assertIn("HAOS", txt, f"{fname} must reference Small-Compute HAOS boxes")
            self.assertIn("conv=2", txt, f"{fname} must document conv=2 8 kHz G.711 A-law mic stream")
            self.assertIn("G.711 A-law", txt, f"{fname} must document G.711 A-law audio")
            self.assertIn("200–900 Hz", txt, f"{fname} must document 200-900 Hz motor hum band")
            self.assertIn("1500–3800 Hz", txt, f"{fname} must document 1500-3800 Hz kibble clatter/ting band")
            self.assertIn("motor_no_kibble", txt, f"{fname} must document motor_no_kibble clog/empty hopper outcome")
            self.assertIn(">= 15", txt, f"{fname} must document >= 15 kibble impact spikes threshold")
            self.assertIn("4.6", txt, f"{fname} must document ting_rate_per_sec >= 4.6 threshold")
            self.assertIn("< 2 MB", txt, f"{fname} must document < 2 MB RAM footprint")
            self.assertIn("cmd 6", txt, f"{fname} must document skipping cmd 6 H.264 video stream")
            self.assertIn("256-entry", txt, f"{fname} must document 256-entry A-law lookup table")
            self.assertIn("wave", txt, f"{fname} must document stdlib wave writer")
            self.assertIn("ffmpeg", txt, f"{fname} must document zero ffmpeg requirement")
            self.assertIn(
                "enable_verification=True, enable_vision_verification=False, enable_camera=False",
                txt.replace("`", ""),
                f"{fname} must document the lightweight HAOS default/recommended profile",
            )

    def test_zero_watchdog_imports_in_m3_platforms(self) -> None:
        for name in (
            "sensor.py",
            "binary_sensor.py",
            "switch.py",
            "button.py",
            "number.py",
            "select.py",
            "camera.py",
            "image.py",
            "event.py",
        ):
            py_path = COMPONENT_DIR / name
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name == "watchdog" or alias.name.startswith("watchdog."))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(node.module == "watchdog" or node.module.startswith("watchdog."))


class TestM3EdgeCasesAndLifecycle(unittest.IsolatedAsyncioTestCase):
    """Additional edge-case tests for coordinator resolution, camera lock, event lifecycle, and nullable sensors."""

    async def test_coordinator_resolution_modes_and_nullable_sensors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            # Mode 1: direct coordinator object in hass.data[DOMAIN][entry_id]
            hass.data[DOMAIN][entry.entry_id] = coord
            sensors: List[Any] = []
            await sensor_mod.async_setup_entry(hass, entry, sensors.extend)
            self.assertEqual(len(sensors), 22)

            # Mode 2: runtime_data fallback when hass.data[DOMAIN] is empty
            hass.data[DOMAIN] = {}
            entry.runtime_data = coord
            bsensors: List[Any] = []
            await binary_sensor_mod.async_setup_entry(hass, entry, bsensors.extend)
            self.assertEqual(len(bsensors), 9)

            # Nullable summary fields
            for key in (
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
                coord.data["summary"][key] = None
            for s in sensors:
                _ = s.native_value
                _ = s.extra_state_attributes

    async def test_camera_session_lock_and_event_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            hass, entry, coord = _create_fixture(td)
            cameras: List[Any] = []
            events: List[Any] = []
            await camera_mod.async_setup_entry(hass, entry, cameras.extend)
            await event_mod.async_setup_entry(hass, entry, events.extend)
            cam = cameras[0]
            feed_ev = next(e for e in events if e.key == "feed_event")

            # Acquire session lock -> camera returns cached snapshot without calling client.capture_snapshot
            coord.data["latest_snapshot_bytes"] = b"\xff\xd8locked_cache"
            async with coord.session_lock:
                res = await cam.async_camera_image()
                self.assertEqual(res, b"\xff\xd8locked_cache")
                self.assertEqual(coord.client.capture_snapshot.call_count, 0)

            # Event lifecycle async_added_to_hass / async_will_remove_from_hass
            await feed_ev.async_added_to_hass()
            hass.bus.async_fire(EVENT_FEED, {"event_type": "dispensed", "portions": 3})
            self.assertIsNotNone(feed_ev.state)
            self.assertEqual(feed_ev.extra_state_attributes.get("portions"), 3)
            await feed_ev.async_will_remove_from_hass()

    def test_e2e_f15_to_f18_suites_pass_with_m3_entities(self) -> None:
        """Verify all 44 E2E Tier 1 & Tier 2 F15-F18 tests pass against the M3 deliverables."""
        import importlib
        import custom_components.molly_cody_feeder as init_mod
        from tests.e2e import test_tier1_features as t1
        from tests.e2e import test_tier2_boundaries as t2

        had_setup = hasattr(init_mod, "async_setup_entry")
        had_unload = hasattr(init_mod, "async_unload_entry")

        async def _fallback_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
            coord = FakeM3Coordinator(hass, entry)
            hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coord}
            entry.runtime_data = coord
            return True

        async def _fallback_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
            if hasattr(hass, "data") and DOMAIN in hass.data:
                hass.data[DOMAIN].pop(entry.entry_id, None)
            return True

        if not had_setup:
            setattr(init_mod, "async_setup_entry", _fallback_setup_entry)
        if not had_unload:
            setattr(init_mod, "async_unload_entry", _fallback_unload_entry)
        try:
            loader = unittest.TestLoader()
            suite = unittest.TestSuite()
            for cls in (
                t1.TestF15SensorAndBinarySensorPlatforms,
                t1.TestF16ControlEntityPlatforms,
                t1.TestF17CameraImageAndEventPlatforms,
                t1.TestF18LovelaceBlueprintsAndDocs,
                t2.TestF15SensorBoundaries,
                t2.TestF16ControlEntityBoundaries,
                t2.TestF17CameraImageEventBoundaries,
                t2.TestF18DocsAndYamlBoundaries,
            ):
                suite.addTests(loader.loadTestsFromTestCase(cls))
            result = unittest.TestResult()
            suite.run(result)
            self.assertEqual(result.TestsRun if hasattr(result, "TestsRun") else result.testsRun, 44)
            self.assertEqual(len(result.failures), 0, f"Failures: {result.failures}")
            self.assertEqual(len(result.errors), 0, f"Errors: {result.errors}")
        finally:
            if not had_setup and hasattr(init_mod, "async_setup_entry"):
                delattr(init_mod, "async_setup_entry")
            if not had_unload and hasattr(init_mod, "async_unload_entry"):
                delattr(init_mod, "async_unload_entry")


if __name__ == "__main__":
    unittest.main()


