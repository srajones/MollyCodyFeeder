"""Event platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EventEntity,
    EventEntityDescription,
    HomeAssistant,
    callback,
    dt_util,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    EVENT_ALERT,
    EVENT_FEED,
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


FEED_EVENT_TYPES: List[str] = [
    "dispensed",
    "motor_no_kibble",
    "no_motor",
    "unverified",
    "camera_unavailable",
    "bowl_check",
    "recording_cut_short",
    "manual_feed",
    "feed_event",
    "fed",
    "weak_flow",
    "problem",
]

ALERT_EVENT_TYPES: List[str] = [
    "clog_detected",
    "hardware_fault",
    "hopper_low",
    "hopper_empty_suspected",
    "weak_flow",
    "schedule_changed",
    "schedule_empty",
    "offline",
    "no_food_hours",
    "alert_event",
    "problem",
]


@dataclass(frozen=True)
class MollyCodyEventEntityDescription(EventEntityDescription):
    """Describes a Molly & Cody Smart Feeder event entity."""

    bus_event_name: str = ""
    coord_data_key: str = ""
    default_event_type: str = ""


EVENT_DESCRIPTIONS: tuple[MollyCodyEventEntityDescription, ...] = (
    # 1. Feed Event Entity
    MollyCodyEventEntityDescription(
        key="feed_event",
        name="Feed Event",
        icon="mdi:food-drumstick",
        event_types=FEED_EVENT_TYPES,
        bus_event_name=EVENT_FEED,
        coord_data_key="last_feed_event",
        default_event_type="feed_event",
    ),
    # 2. Alert Event Entity
    MollyCodyEventEntityDescription(
        key="alert_event",
        name="Alert Event",
        icon="mdi:bell-alert-outline",
        event_types=ALERT_EVENT_TYPES,
        bus_event_name=EVENT_ALERT,
        coord_data_key="last_alert_event",
        default_event_type="alert_event",
    ),
)


class MollyCodyEventEntity(CoordinatorEntity[Dict[str, Any]], EventEntity):
    """Event entity tracking feed verification outcomes and feeder alerts."""

    entity_description: MollyCodyEventEntityDescription

    def __init__(
        self,
        hass: Optional[HomeAssistant],
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodyEventEntityDescription,
    ) -> None:
        """Initialize the event entity and subscribe to the Home Assistant event bus."""
        super().__init__(coordinator)
        if hass is not None:
            self.hass = hass
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key
        self._attr_event_types = list(description.event_types or [])
        self._unsub_bus: Optional[Callable[[], None]] = None
        self._ensure_bus_subscription()

    @property
    def key(self) -> str:
        """Return the event entity key."""
        return self.entity_description.key

    def _ensure_bus_subscription(self) -> None:
        """Subscribe to hass.bus once without duplicate subscriptions."""
        if self._unsub_bus is not None:
            return
        hass_obj = self.hass or getattr(self.coordinator, "hass", None)
        if hass_obj is not None and hasattr(hass_obj, "bus") and hasattr(hass_obj.bus, "async_listen"):
            self._unsub_bus = hass_obj.bus.async_listen(
                self.entity_description.bus_event_name,
                self._handle_bus_event,
            )

    async def async_added_to_hass(self) -> None:
        """Register listeners when added to Home Assistant."""
        await super().async_added_to_hass()
        self._ensure_bus_subscription()
        if self._unsub_bus is not None:
            self.async_on_remove(self._unsub_bus)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from bus when removed from Home Assistant."""
        if self._unsub_bus is not None:
            try:
                self._unsub_bus()
            except Exception:
                pass
            self._unsub_bus = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_bus_event(self, event: Any) -> None:
        """Handle an incoming event on the Home Assistant bus."""
        payload = getattr(event, "data", None)
        if not isinstance(payload, dict):
            payload = dict(event) if isinstance(event, dict) else {}
        raw_type = str(
            payload.get("event_type")
            or payload.get("outcome")
            or payload.get("result")
            or self.entity_description.default_event_type
        )
        ev_type = raw_type if raw_type in self.event_types else self.entity_description.default_event_type
        self._trigger_event(ev_type, payload)

    @property
    def state(self) -> Optional[str]:
        """Return the ISO timestamp of the last triggered event (or from coordinator.data)."""
        if self._last_event_time is not None:
            return self._last_event_time
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict):
            last_ev = data.get(self.entity_description.coord_data_key)
            if isinstance(last_ev, dict) and last_ev:
                ts = last_ev.get("timestamp") or last_ev.get("ts") or last_ev.get("time")
                if ts:
                    return str(ts)
                return dt_util.utcnow().isoformat()
        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return event attributes from either the triggered bus event or coordinator.data."""
        attrs = dict(super().extra_state_attributes or {})
        if not self._last_event_attributes:
            data = getattr(self.coordinator, "data", None)
            if isinstance(data, dict):
                last_ev = data.get(self.entity_description.coord_data_key)
                if isinstance(last_ev, dict) and last_ev:
                    ev_type = str(
                        last_ev.get("event_type")
                        or last_ev.get("outcome")
                        or self.entity_description.default_event_type
                    )
                    attrs.setdefault("event_type", ev_type)
                    attrs.update(last_ev)
        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 2 Molly & Cody Smart Feeder event entities from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[MollyCodyEventEntity] = [
        MollyCodyEventEntity(hass, coordinator, entry, desc)
        for desc in EVENT_DESCRIPTIONS
    ]
    async_add_entities(entities)
