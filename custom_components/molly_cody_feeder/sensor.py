"""Sensor platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityCategory,
    HomeAssistant,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
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


def _build_device_info(coordinator: Any, entry: ConfigEntry) -> TupleDeviceInfo:
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


TupleDeviceInfo = tuple[str, DeviceInfo]


@dataclass(frozen=True)
class MollyCodySensorEntityDescription(SensorEntityDescription):
    """Describes a Molly & Cody Smart Feeder sensor entity."""

    value_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None
    attr_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = None


def _schedule_summary_value(data: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """Compute a human-readable summary value for the schedule sensor."""
    next_meal = summary.get("next_meal_time")
    if next_meal:
        return str(next_meal)
    slots = data.get("schedule")
    if isinstance(slots, list):
        active = sum(1 for s in slots if isinstance(s, dict) and s.get("enabled"))
        return f"{active} active slots"
    return "0 active slots"


def _schedule_extra_attrs(data: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Build extra_state_attributes for the schedule sensor."""
    slots = data.get("schedule") if isinstance(data.get("schedule"), list) else []
    enabled_slots = [s for s in slots if isinstance(s, dict) and s.get("enabled")]
    return {
        "schedule_raw": data.get("schedule_raw", ""),
        "slots": slots,
        "enabled_slots": enabled_slots,
        "next_meal_time": summary.get("next_meal_time"),
        "minutes_to_next_meal": summary.get("minutes_to_next_meal"),
    }


def _status_extra_attrs(data: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Build extra_state_attributes for the overall feeder status sensor."""
    return {
        "last_meal_text": summary.get("last_meal_text"),
        "last_meal_outcome": summary.get("last_meal_outcome"),
        "food_weight_g": data.get("food_weight_g"),
        "consecutive_missed_feeds": summary.get("consecutive_missed_feeds", 0),
        "last_alert_title": summary.get("last_alert_title"),
        "last_alert_time": summary.get("last_alert_time"),
    }


def _today_meals_extra_attrs(data: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Build extra_state_attributes for today's meals sensor."""
    today_list = summary.get("today_list")
    return {
        "today_list": today_list if isinstance(today_list, list) else [],
        "week_meals": summary.get("week_meals", 0),
    }


SENSOR_DESCRIPTIONS: tuple[MollyCodySensorEntityDescription, ...] = (
    # 1. Overall Status
    MollyCodySensorEntityDescription(
        key="status",
        name="Status",
        icon="mdi:paw",
        value_fn=lambda data, summary: summary.get("status", "ok"),
        attr_fn=_status_extra_attrs,
    ),
    # 2. Active Transport
    MollyCodySensorEntityDescription(
        key="active_transport",
        name="Active Transport",
        icon="mdi:access-point-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, summary: data.get("active_transport", "none"),
    ),
    # 3. Configured Connection Mode
    MollyCodySensorEntityDescription(
        key="configured_mode",
        name="Connection Mode",
        icon="mdi:wan",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, summary: data.get("configured_mode", "auto"),
    ),
    # 4. Last Meal Time
    MollyCodySensorEntityDescription(
        key="last_meal_time",
        name="Last Meal Time",
        icon="mdi:clock-check-outline",
        value_fn=lambda data, summary: summary.get("last_meal_time"),
    ),
    # 5. Last Meal Result
    MollyCodySensorEntityDescription(
        key="last_meal_result",
        name="Last Meal Result",
        icon="mdi:food-drumstick",
        value_fn=lambda data, summary: summary.get("last_meal_result", "none"),
    ),
    # 6. Last Meal Portions
    MollyCodySensorEntityDescription(
        key="last_meal_portions",
        name="Last Meal Portions",
        icon="mdi:counter",
        native_unit_of_measurement="portions",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("last_meal_portions"),
    ),
    # 7. Minutes Since Last Meal
    MollyCodySensorEntityDescription(
        key="minutes_since_last_meal",
        name="Minutes Since Last Meal",
        icon="mdi:timer-sand",
        native_unit_of_measurement="min",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("minutes_since_last_meal"),
    ),
    # 8. Next Meal Time
    MollyCodySensorEntityDescription(
        key="next_meal_time",
        name="Next Meal Time",
        icon="mdi:clock-outline",
        value_fn=lambda data, summary: summary.get("next_meal_time"),
    ),
    # 9. Minutes To Next Meal
    MollyCodySensorEntityDescription(
        key="minutes_to_next_meal",
        name="Minutes To Next Meal",
        icon="mdi:timer-outline",
        native_unit_of_measurement="min",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("minutes_to_next_meal"),
    ),
    # 10. Today Meals
    MollyCodySensorEntityDescription(
        key="today_meals",
        name="Today Meals",
        icon="mdi:silverware-fork-knife",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data, summary: summary.get("today_meals", 0),
        attr_fn=_today_meals_extra_attrs,
    ),
    # 11. Today Fed
    MollyCodySensorEntityDescription(
        key="today_fed",
        name="Today Confirmed Meals",
        icon="mdi:check-circle-outline",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data, summary: summary.get("today_fed", 0),
    ),
    # 12. Today Problems
    MollyCodySensorEntityDescription(
        key="today_problems",
        name="Today Failed Meals",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data, summary: summary.get("today_problems", 0),
    ),
    # 13. Today Portions
    MollyCodySensorEntityDescription(
        key="today_portions",
        name="Today Portions",
        icon="mdi:scale-balance",
        native_unit_of_measurement="portions",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda data, summary: summary.get("today_portions", 0),
    ),
    # 14. 7-Day Confirmed Rate
    MollyCodySensorEntityDescription(
        key="week_confirmed_pct",
        name="7d Confirmed Rate",
        icon="mdi:percent-outline",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("week_confirmed_pct"),
    ),
    # 15. Kibble Flow (Tings/s)
    MollyCodySensorEntityDescription(
        key="kibble_flow",
        name="Kibble Flow Rate",
        icon="mdi:waveform",
        native_unit_of_measurement="tings/s",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("kibble_flow"),
    ),
    # 16. 7-Day Average Kibble Flow
    MollyCodySensorEntityDescription(
        key="kibble_flow_7d_avg",
        name="7d Avg Kibble Flow",
        icon="mdi:chart-line",
        native_unit_of_measurement="tings/s",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("kibble_flow_7d_avg"),
    ),
    # 17. Last Motor Duration
    MollyCodySensorEntityDescription(
        key="last_motor_duration_sec",
        name="Last Motor Duration",
        icon="mdi:engine-outline",
        native_unit_of_measurement="s",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("last_motor_duration_sec"),
    ),
    # 18. Hopper Percent
    MollyCodySensorEntityDescription(
        key="hopper_percent",
        name="Hopper Level",
        icon="mdi:gauge",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("hopper_percent", 100.0),
    ),
    # 19. Hopper Portions Left
    MollyCodySensorEntityDescription(
        key="hopper_portions_left",
        name="Hopper Portions Left",
        icon="mdi:grain",
        native_unit_of_measurement="portions",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("hopper_portions_left", 0),
    ),
    # 20. Hopper Days Left
    MollyCodySensorEntityDescription(
        key="hopper_days_left",
        name="Hopper Days Left",
        icon="mdi:calendar-clock",
        native_unit_of_measurement="d",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("hopper_days_left"),
    ),
    # 21. Bowl Fill Percentage
    MollyCodySensorEntityDescription(
        key="bowl_fill_pct",
        name="Bowl Fill",
        icon="mdi:bowl-mix-outline",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data, summary: summary.get("bowl_fill_pct"),
    ),
    # 22. Schedule Summary
    MollyCodySensorEntityDescription(
        key="schedule",
        name="Meal Schedule",
        icon="mdi:calendar-Multiselect",
        value_fn=_schedule_summary_value,
        attr_fn=_schedule_extra_attrs,
    ),
)


class MollyCodySensorEntity(CoordinatorEntity[Dict[str, Any]], SensorEntity):
    """Representation of a Molly & Cody Smart Feeder sensor."""

    entity_description: MollyCodySensorEntityDescription

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodySensorEntityDescription,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class

    @property
    def key(self) -> str:
        """Return the sensor description key."""
        return self.entity_description.key

    @property
    def native_value(self) -> Any:
        """Return the current sensor value dynamically from coordinator.data."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return None
        summary = data.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(data, summary)
        return summary.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        """Return optional extra state attributes dynamically from coordinator.data."""
        if self.entity_description.attr_fn is None:
            return self._attr_extra_state_attributes
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return {}
        summary = data.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        return self.entity_description.attr_fn(data, summary)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 22 Molly & Cody Smart Feeder sensors from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[MollyCodySensorEntity] = [
        MollyCodySensorEntity(coordinator, entry, desc) for desc in SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)
