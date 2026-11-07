"""Button platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ._ha_compat import (
    ButtonEntity,
    ButtonEntityDescription,
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
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


@dataclass(frozen=True)
class MollyCodyButtonEntityDescription(ButtonEntityDescription):
    """Describes a Molly & Cody Smart Feeder button entity."""

    press_fn: Optional[Callable[[Any], Awaitable[Any]]] = None


BUTTON_DESCRIPTIONS: tuple[MollyCodyButtonEntityDescription, ...] = (
    # 1. Feed 1 Portion (Unverified instant feed)
    MollyCodyButtonEntityDescription(
        key="feed_1_portion",
        name="Feed 1p",
        icon="mdi:food-drumstick",
        press_fn=lambda coord: coord.async_feed(portions=1, verify=False),
    ),
    # 2. Feed & Verify (Camera + Mic anti-clog verification)
    MollyCodyButtonEntityDescription(
        key="verify_feed",
        name="Feed & Verify",
        icon="mdi:shield-check-outline",
        press_fn=lambda coord: coord.async_verify_feed(),
    ),
    # 3. Check Bowl (On-demand bowl inspection)
    MollyCodyButtonEntityDescription(
        key="check_bowl",
        name="Check Bowl",
        icon="mdi:bowl-mix-outline",
        press_fn=lambda coord: coord.async_check_bowl(),
    ),
    # 4. Capture Snapshot
    MollyCodyButtonEntityDescription(
        key="capture_snapshot",
        name="Snapshot",
        icon="mdi:camera-outline",
        press_fn=lambda coord: coord.async_capture_snapshot(),
    ),
    # 5. Refill Hopper
    MollyCodyButtonEntityDescription(
        key="refill_hopper",
        name="Refill Hopper",
        icon="mdi:tray-full",
        press_fn=lambda coord: coord.async_refill_hopper(),
    ),
    # 6. Play Chime
    MollyCodyButtonEntityDescription(
        key="play_chime",
        name="Chime",
        icon="mdi:bell-ring-outline",
        press_fn=lambda coord: coord.async_play_chime(),
    ),
    # 7. Sync Schedules
    MollyCodyButtonEntityDescription(
        key="sync_schedules",
        name="Sync Schedule",
        icon="mdi:calendar-sync",
        press_fn=lambda coord: coord.async_sync_schedules(),
    ),
    # 8. Clear Clog Alert
    MollyCodyButtonEntityDescription(
        key="clear_clog_alert",
        name="Clear Alert",
        icon="mdi:alert-remove-outline",
        press_fn=lambda coord: coord.async_clear_clog_alert(),
    ),
)


class MollyCodyButtonEntity(CoordinatorEntity[Dict[str, Any]], ButtonEntity):
    """Representation of a Molly & Cody Smart Feeder action button."""

    entity_description: MollyCodyButtonEntityDescription

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodyButtonEntityDescription,
    ) -> None:
        """Initialize the button entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key

    @property
    def key(self) -> str:
        """Return the button key."""
        return self.entity_description.key

    async def async_press(self) -> None:
        """Execute the button press action via the coordinator."""
        if self.entity_description.press_fn is not None:
            await self.entity_description.press_fn(self.coordinator)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 8 Molly & Cody Smart Feeder buttons from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[MollyCodyButtonEntity] = [
        MollyCodyButtonEntity(coordinator, entry, desc) for desc in BUTTON_DESCRIPTIONS
    ]
    async_add_entities(entities)
