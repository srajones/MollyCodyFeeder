"""Number platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityCategory,
    HomeAssistant,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_HOPPER_CAPACITY,
    CONF_PORTION_WEIGHT_G,
    CONF_TTS_VOLUME,
    DEFAULT_HOPPER_CAPACITY,
    DEFAULT_PORTION_WEIGHT_G,
    DEFAULT_TTS_VOLUME,
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


def _get_option_value(coordinator: Any, key: str, default: Any) -> Any:
    """Read an option value from coordinator.data['options'] or coordinator.options."""
    data = getattr(coordinator, "data", None)
    if isinstance(data, dict) and isinstance(data.get("options"), dict):
        if key in data["options"] and data["options"][key] is not None:
            return data["options"][key]
    opts = getattr(coordinator, "options", None)
    if isinstance(opts, dict) and key in opts and opts[key] is not None:
        return opts[key]
    return default


@dataclass(frozen=True)
class MollyCodyNumberEntityDescription(NumberEntityDescription):
    """Describes a Molly & Cody Smart Feeder config/control number entity."""

    option_key: str = ""
    is_int: bool = True
    default_value: float = 1.0


CONFIG_NUMBER_DESCRIPTIONS: tuple[MollyCodyNumberEntityDescription, ...] = (
    # 1. Manual Feed Portions (1..20)
    MollyCodyNumberEntityDescription(
        key="feed_portions",
        name="Manual Feed Portions",
        icon="mdi:food-drumstick",
        native_min_value=1,
        native_max_value=20,
        native_step=1,
        native_unit_of_measurement="portions",
        mode=NumberMode.BOX,
        option_key="manual_feed_portions",
        is_int=True,
        default_value=1,
    ),
    # 2. Hopper Capacity (10..500 portions)
    MollyCodyNumberEntityDescription(
        key="hopper_capacity_portions",
        name="Hopper Capacity",
        icon="mdi:grain",
        entity_category=EntityCategory.CONFIG,
        native_min_value=10,
        native_max_value=500,
        native_step=1,
        native_unit_of_measurement="portions",
        mode=NumberMode.BOX,
        option_key=CONF_HOPPER_CAPACITY,
        is_int=True,
        default_value=DEFAULT_HOPPER_CAPACITY,
    ),
    # 3. Portion Weight (1..50 g)
    MollyCodyNumberEntityDescription(
        key="portion_weight_g",
        name="Portion Weight",
        icon="mdi:weight-gram",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=50,
        native_step=1,
        native_unit_of_measurement="g",
        mode=NumberMode.BOX,
        option_key=CONF_PORTION_WEIGHT_G,
        is_int=True,
        default_value=DEFAULT_PORTION_WEIGHT_G,
    ),
    # 4. TTS Volume (0.1..5.0)
    MollyCodyNumberEntityDescription(
        key="tts_volume",
        name="TTS Volume",
        icon="mdi:volume-high",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0.1,
        native_max_value=5.0,
        native_step=0.1,
        mode=NumberMode.SLIDER,
        option_key=CONF_TTS_VOLUME,
        is_int=False,
        default_value=DEFAULT_TTS_VOLUME,
    ),
)


class MollyCodyConfigNumberEntity(CoordinatorEntity[Dict[str, Any]], NumberEntity):
    """Number entity for feeder control and configuration parameters."""

    entity_description: MollyCodyNumberEntityDescription

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodyNumberEntityDescription,
    ) -> None:
        """Initialize the config number entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key
        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_mode = description.mode
        self._local_value: float = description.default_value

    @property
    def key(self) -> str:
        """Return the number description key."""
        return self.entity_description.key

    @property
    def native_value(self) -> float:
        """Return the current number value (never None)."""
        data = getattr(self.coordinator, "data", None)
        opt_key = self.entity_description.option_key
        if opt_key == CONF_HOPPER_CAPACITY and isinstance(data, dict):
            summary = data.get("summary")
            if isinstance(summary, dict) and summary.get("hopper_capacity_portions") is not None:
                val = summary.get("hopper_capacity_portions")
                return int(val) if self.entity_description.is_int else float(val)
        raw = _get_option_value(self.coordinator, opt_key, None)
        if raw is None and opt_key == "manual_feed_portions" and isinstance(data, dict):
            raw = data.get("last_feed_portions") or self._local_value
        if raw is None:
            raw = self._local_value
        try:
            num = int(round(float(raw))) if self.entity_description.is_int else float(raw)
        except (TypeError, ValueError):
            num = int(self.entity_description.default_value) if self.entity_description.is_int else float(self.entity_description.default_value)
        low = self.entity_description.native_min_value
        high = self.entity_description.native_max_value
        clamped = max(low, min(high, num))
        return int(clamped) if self.entity_description.is_int else float(clamped)

    async def async_set_native_value(self, value: float) -> None:
        """Set a new number value and update coordinator options."""
        low = self.entity_description.native_min_value
        high = self.entity_description.native_max_value
        clamped = max(low, min(high, value))
        typed_val: Any = int(round(clamped)) if self.entity_description.is_int else round(float(clamped), 2)
        self._local_value = float(typed_val)
        opt_key = self.entity_description.option_key

        if hasattr(self.coordinator, "options") and isinstance(self.coordinator.options, dict):
            self.coordinator.options[opt_key] = typed_val
        if isinstance(getattr(self.coordinator, "data", None), dict):
            self.coordinator.data.setdefault("options", {})[opt_key] = typed_val
            if opt_key == CONF_HOPPER_CAPACITY and isinstance(self.coordinator.data.get("summary"), dict):
                self.coordinator.data["summary"]["hopper_capacity_portions"] = int(typed_val)
        if hasattr(self.coordinator, "async_set_option"):
            await self.coordinator.async_set_option(opt_key, typed_val)
        self.async_write_ha_state()


class MollyCodyScheduleSlotPortionsNumber(CoordinatorEntity[Dict[str, Any]], NumberEntity):
    """Number entity controlling the portion count (1..20) for a meal schedule slot (Slots 1..8)."""

    def __init__(self, coordinator: Any, entry: ConfigEntry, slot_num: int) -> None:
        """Initialize the schedule slot portions number entity."""
        super().__init__(coordinator)
        self._slot_num = int(slot_num)
        key = f"slot_{self._slot_num}_portions"
        self.entity_description = NumberEntityDescription(
            key=key,
            name=f"Schedule Slot {self._slot_num} Portions",
            icon="mdi:counter",
            native_min_value=1,
            native_max_value=20,
            native_step=1,
            native_unit_of_measurement="portions",
            mode=NumberMode.BOX,
            translation_key=key,
        )
        self._key = key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = self.entity_description.name
        self._attr_device_info = device_info
        self._attr_translation_key = key
        self._attr_native_min_value = 1
        self._attr_native_max_value = 20
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "portions"
        self._attr_mode = NumberMode.BOX

    @property
    def key(self) -> str:
        """Return the number entity key."""
        return self._key

    @property
    def native_value(self) -> int:
        """Return the current portion count for this schedule slot (always int 1..20, never None)."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return 1
        slot_dict = _find_slot_dict(data.get("schedule"), self._slot_num)
        if slot_dict is None:
            return 1
        try:
            portions = int(slot_dict.get("portions", 1) or 1)
        except (TypeError, ValueError):
            portions = 1
        return max(1, min(20, portions))

    async def async_set_native_value(self, value: float) -> None:
        """Update the portion count for this schedule slot on DP 207."""
        portions = max(1, min(20, int(round(float(value)))))
        await self.coordinator.async_set_schedule_slot(slot=self._slot_num, portions=portions)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 12 Molly & Cody Smart Feeder number entities from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[NumberEntity] = []

    # 1..4: 4 Config/Control Numbers
    for desc in CONFIG_NUMBER_DESCRIPTIONS:
        entities.append(MollyCodyConfigNumberEntity(coordinator, entry, desc))

    # 5..12: 8 Schedule Slot Portion Numbers (Slots 1..8)
    for slot_num in range(1, 9):
        entities.append(MollyCodyScheduleSlotPortionsNumber(coordinator, entry, slot_num))

    async_add_entities(entities)
