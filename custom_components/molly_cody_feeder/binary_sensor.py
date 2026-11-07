"""Binary sensor platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityCategory,
    HomeAssistant,
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


def _is_online(data: Dict[str, Any], summary: Dict[str, Any]) -> bool:
    """Return True when the feeder is online and has an active transport."""
    online = bool(data.get("online", False))
    transport = str(data.get("active_transport", "none")).lower()
    return bool(online and transport != "none")


def _is_hardware_fault(data: Dict[str, Any], summary: Dict[str, Any]) -> bool:
    """Return True only when DP 255 (`feed_abnormal`) > 1 (1 is normal, >= 2 is fault)."""
    val = data.get("feed_abnormal")
    if val is None and isinstance(data.get("dps"), dict):
        val = data["dps"].get("255", data["dps"].get(255))
    try:
        return int(val) > 1
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class MollyCodyBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Molly & Cody Smart Feeder binary sensor entity."""

    is_on_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], bool]] = None


BINARY_SENSOR_DESCRIPTIONS: tuple[MollyCodyBinarySensorEntityDescription, ...] = (
    # 1. Online
    MollyCodyBinarySensorEntityDescription(
        key="online",
        name="Online",
        icon="mdi:wifi-check",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_is_online,
    ),
    # 2. Clog Detected
    MollyCodyBinarySensorEntityDescription(
        key="clog_detected",
        name="Clog Detected",
        icon="mdi:alert-octagon",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data, summary: bool(summary.get("clog_detected", False)),
    ),
    # 3. Hardware Fault (DP 255 > 1)
    MollyCodyBinarySensorEntityDescription(
        key="hardware_fault",
        name="Hardware Fault",
        icon="mdi:cog-off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_is_hardware_fault,
    ),
    # 4. Hopper Low
    MollyCodyBinarySensorEntityDescription(
        key="hopper_low",
        name="Hopper Low",
        icon="mdi:tray-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data, summary: bool(summary.get("hopper_low", False)),
    ),
    # 5. Food Present
    MollyCodyBinarySensorEntityDescription(
        key="food_present",
        name="Food Present",
        icon="mdi:food-drumstick",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        is_on_fn=lambda data, summary: bool(summary.get("food_present", False)),
    ),
    # 6. Dog at Bowl
    MollyCodyBinarySensorEntityDescription(
        key="dog_at_bowl",
        name="Dog at Bowl",
        icon="mdi:dog-side",
        device_class=BinarySensorDeviceClass.MOTION,
        is_on_fn=lambda data, summary: bool(summary.get("dog_at_bowl", False)),
    ),
    # 7. Infrared Night-Vision Mode
    MollyCodyBinarySensorEntityDescription(
        key="is_infrared_mode",
        name="Infrared Mode",
        icon="mdi:weather-night",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data, summary: bool(summary.get("is_infrared_mode", False)),
    ),
    # 8. Busy (Session in Progress)
    MollyCodyBinarySensorEntityDescription(
        key="busy",
        name="Busy",
        icon="mdi:progress-clock",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data, summary: bool(summary.get("busy", False)),
    ),
    # 9. Weak Flow Warning
    MollyCodyBinarySensorEntityDescription(
        key="weak_flow_warning",
        name="Weak Flow Warning",
        icon="mdi:water-alert",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data, summary: bool(summary.get("weak_flow_warning", False)),
    ),
)


class MollyCodyBinarySensorEntity(CoordinatorEntity[Dict[str, Any]], BinarySensorEntity):
    """Representation of a Molly & Cody Smart Feeder binary sensor."""

    entity_description: MollyCodyBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodyBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key
        self._attr_device_class = description.device_class

    @property
    def key(self) -> str:
        """Return the binary sensor key."""
        return self.entity_description.key

    @property
    def is_on(self) -> bool:
        """Return True if the binary sensor is on (always bool, never None)."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return False
        summary = data.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        if self.entity_description.is_on_fn is not None:
            return bool(self.entity_description.is_on_fn(data, summary))
        return bool(summary.get(self.entity_description.key, False))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 9 Molly & Cody Smart Feeder binary sensors from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[MollyCodyBinarySensorEntity] = [
        MollyCodyBinarySensorEntity(coordinator, entry, desc)
        for desc in BINARY_SENSOR_DESCRIPTIONS
    ]
    async_add_entities(entities)
