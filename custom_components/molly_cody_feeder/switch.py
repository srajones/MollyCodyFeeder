"""Switch platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityCategory,
    HomeAssistant,
    SwitchEntity,
    SwitchEntityDescription,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_ENABLE_AUTO_RETRY,
    CONF_ENABLE_TTS,
    CONF_ENABLE_VERIFICATION,
    CONF_ENABLE_VISION_VERIFICATION,
    DEFAULT_ENABLE_AUTO_RETRY,
    DEFAULT_ENABLE_TTS,
    DEFAULT_ENABLE_VERIFICATION,
    DEFAULT_ENABLE_VISION_VERIFICATION,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    NAME,
    VERSION,
)


def _resolve_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> Any:
    """Resolve the coordinator instance from hass.data or entry.runtime_data."""
    domain_data = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    stored = domain_data.get(entry.entry_id)
    if isinstance(stored, dict) and "coordinator" in stored:
        return stored["coordinator"]
    if hasattr(stored, "coordinator"):
        return stored.coordinator
    if stored is not None:
        return stored
    return getattr(entry, "runtime_data", None)


def _build_device_info(coordinator: Any, entry: ConfigEntry) -> tuple[str, DeviceInfo]:
    """Build DeviceInfo and resolve device_id for an entity."""
    data = getattr(coordinator, "data", None) or {}
    entry_data = getattr(entry, "data", None) or {}
    device_id = str(
        data.get("device_id")
        or entry_data.get(CONF_DEVICE_ID)
        or getattr(entry, "unique_id", None)
        or getattr(entry, "entry_id", "molly_cody_feeder")
    )
    device_name = str(
        data.get("device_name")
        or entry_data.get(CONF_DEVICE_NAME)
        or getattr(entry, "title", None)
        or NAME
    )
    info = DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=device_name,
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=VERSION,
    )
    return device_id, info


def _find_slot_dict(schedule: Any, slot_num: int) -> Optional[Dict[str, Any]]:
    """Safely find slot `slot_num` (1..8) in `schedule` without raising IndexError."""
    if not isinstance(schedule, list) or not schedule:
        return None
    for item in schedule:
        if isinstance(item, dict) and item.get("slot") == slot_num:
            return item
    idx = slot_num - 1
    if 0 <= idx < len(schedule) and isinstance(schedule[idx], dict):
        return schedule[idx]
    return None


class MollyCodyScheduleSlotSwitch(CoordinatorEntity[Dict[str, Any]], SwitchEntity):
    """Switch entity enabling or disabling a feeder meal schedule slot (Slots 1..8)."""

    def __init__(self, coordinator: Any, entry: ConfigEntry, slot_num: int) -> None:
        """Initialize the schedule slot enable switch."""
        super().__init__(coordinator)
        self._slot_num = int(slot_num)
        key = f"slot_{self._slot_num}_enabled"
        self.entity_description = SwitchEntityDescription(
            key=key,
            name=f"Schedule Slot {self._slot_num} Enabled",
            icon="mdi:clock-check-outline",
            translation_key=key,
        )
        self._key = key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = self.entity_description.name
        self._attr_device_info = device_info
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the switch key."""
        return self._key

    @property
    def is_on(self) -> bool:
        """Return True if the schedule slot is enabled."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return False
        slot_dict = _find_slot_dict(data.get("schedule"), self._slot_num)
        if slot_dict is None:
            return False
        return bool(slot_dict.get("enabled", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable this schedule slot on DP 207."""
        await self.coordinator.async_set_schedule_slot(slot=self._slot_num, enabled=True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable this schedule slot on DP 207."""
        await self.coordinator.async_set_schedule_slot(slot=self._slot_num, enabled=False)
        self.async_write_ha_state()


class MollyCodyDpSwitch(CoordinatorEntity[Dict[str, Any]], SwitchEntity):
    """Switch entity controlling a boolean Tuya Data Point (DP 101, 103, 104, 134)."""

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        key: str,
        name: str,
        dp_id: int,
        icon: str,
    ) -> None:
        """Initialize the hardware DP switch."""
        super().__init__(coordinator)
        self._dp_id = int(dp_id)
        self._key = key
        self.entity_description = SwitchEntityDescription(
            key=key,
            name=name,
            icon=icon,
            entity_category=EntityCategory.CONFIG,
            translation_key=key,
        )
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the switch key."""
        return self._key

    @property
    def is_on(self) -> bool:
        """Return True if the hardware DP is currently enabled."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return False
        dps = data.get("dps")
        if not isinstance(dps, dict):
            return False
        val = dps.get(str(self._dp_id), dps.get(self._dp_id, False))
        return bool(val)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the hardware DP."""
        await self.coordinator.async_set_dp(self._dp_id, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the hardware DP."""
        await self.coordinator.async_set_dp(self._dp_id, False)
        self.async_write_ha_state()


class MollyCodyOptionSwitch(CoordinatorEntity[Dict[str, Any]], SwitchEntity):
    """Switch entity controlling a runtime modular compute / feature toggle option."""

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        key: str,
        option_key: str,
        name: str,
        default: bool,
        icon: str,
    ) -> None:
        """Initialize the option toggle switch."""
        super().__init__(coordinator)
        self._key = key
        self._option_key = option_key
        self._default = bool(default)
        self.entity_description = SwitchEntityDescription(
            key=key,
            name=name,
            icon=icon,
            entity_category=EntityCategory.CONFIG,
            translation_key=key,
        )
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the switch key."""
        return self._key

    @property
    def is_on(self) -> bool:
        """Return True if the runtime option is enabled."""
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict) and isinstance(data.get("options"), dict):
            if self._option_key in data["options"]:
                return bool(data["options"][self._option_key])
        coord_opts = getattr(self.coordinator, "options", None)
        if isinstance(coord_opts, dict) and self._option_key in coord_opts:
            return bool(coord_opts[self._option_key])
        return self._default

    async def _set_option_state(self, state: bool) -> None:
        """Update the option via coordinator.async_set_option and sync local dicts."""
        if hasattr(self.coordinator, "async_set_option"):
            await self.coordinator.async_set_option(self._option_key, bool(state))
        if hasattr(self.coordinator, "options") and isinstance(self.coordinator.options, dict):
            self.coordinator.options[self._option_key] = bool(state)
        if isinstance(getattr(self.coordinator, "data", None), dict):
            self.coordinator.data.setdefault("options", {})[self._option_key] = bool(state)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the feature toggle."""
        await self._set_option_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the feature toggle."""
        await self._set_option_state(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 16 Molly & Cody Smart Feeder switches from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[SwitchEntity] = []

    # 1..8: Schedule Slot 1..8 Enable Switches
    for slot_num in range(1, 9):
        entities.append(MollyCodyScheduleSlotSwitch(coordinator, entry, slot_num))

    # 9..12: 4 Hardware DP Switches (DP 101, 103, 104, 134)
    dp_switches = (
        ("indicator_light", "Indicator Light", 101, "mdi:led-on"),
        ("flip_screen", "Flip Screen", 103, "mdi:flip-vertical"),
        ("osd_watermark", "OSD Watermark", 104, "mdi:watermark"),
        ("motion_switch", "Motion Detection", 134, "mdi:motion-sensor"),
    )
    for key, name, dp_id, icon in dp_switches:
        entities.append(MollyCodyDpSwitch(coordinator, entry, key, name, dp_id, icon))

    # 13..16: 4 Modular Compute / Feature Toggle Switches
    option_switches = (
        ("enable_auto_retry", CONF_ENABLE_AUTO_RETRY, "Auto-Retry on Clog", DEFAULT_ENABLE_AUTO_RETRY, "mdi:restart-alert"),
        ("enable_tts", CONF_ENABLE_TTS, "Speaker TTS Alerts", DEFAULT_ENABLE_TTS, "mdi:bullhorn-outline"),
        ("enable_verification", CONF_ENABLE_VERIFICATION, "Audio Anti-Clog Verification", DEFAULT_ENABLE_VERIFICATION, "mdi:microphone-Check"),
        ("enable_vision_verification", CONF_ENABLE_VISION_VERIFICATION, "Vision Bowl Verification", DEFAULT_ENABLE_VISION_VERIFICATION, "mdi:eye-check-outline"),
    )
    for key, opt_key, name, default, icon in option_switches:
        entities.append(MollyCodyOptionSwitch(coordinator, entry, key, opt_key, name, default, icon))

    async_add_entities(entities)
