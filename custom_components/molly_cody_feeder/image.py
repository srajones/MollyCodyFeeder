"""Image platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    HomeAssistant,
    ImageEntity,
    ImageEntityDescription,
    dt_util,
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
class MollyCodyImageEntityDescription(ImageEntityDescription):
    """Describes a Molly & Cody Smart Feeder verification image entity."""

    byte_key: str = ""


IMAGE_DESCRIPTIONS: tuple[MollyCodyImageEntityDescription, ...] = (
    # 1. Annotated Bowl ROI Image
    MollyCodyImageEntityDescription(
        key="annotated_bowl",
        name="Annotated Bowl",
        icon="mdi:image-filter-center-focus",
        byte_key="latest_annotated_bytes",
    ),
    # 2. Before Feed Frame
    MollyCodyImageEntityDescription(
        key="before_feed",
        name="Before Feed",
        icon="mdi:image-outline",
        byte_key="latest_before_bytes",
    ),
    # 3. After Feed Frame
    MollyCodyImageEntityDescription(
        key="after_feed",
        name="After Feed",
        icon="mdi:image-check-outline",
        byte_key="latest_after_bytes",
    ),
)


class MollyCodyVerificationImageEntity(CoordinatorEntity[Dict[str, Any]], ImageEntity):
    """Image entity exposing the latest annotated, before-feed, or after-feed JPEG bytes."""

    entity_description: MollyCodyImageEntityDescription

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        description: MollyCodyImageEntityDescription,
    ) -> None:
        """Initialize the verification image entity."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass=getattr(coordinator, "hass", None))
        self.entity_description = description
        self._key = description.key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{description.key}"
        self._attr_name = description.name
        self._attr_device_info = device_info
        self._attr_translation_key = description.translation_key or description.key
        self._attr_content_type = "image/jpeg"
        self._last_bytes_digest: Optional[int] = None

    @property
    def key(self) -> str:
        """Return the image description key."""
        return self.entity_description.key

    def image(self) -> Optional[bytes]:
        """Return the raw JPEG bytes from coordinator.data, or None before first capture."""
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return None
        raw = data.get(self.entity_description.byte_key)
        if isinstance(raw, (bytes, bytearray)):
            b = bytes(raw)
            digest = hash(b)
            if digest != self._last_bytes_digest:
                self._last_bytes_digest = digest
                self._attr_image_last_updated = dt_util.utcnow()
            return b
        return None

    async def async_image(self) -> Optional[bytes]:
        """Asynchronously return the raw JPEG bytes from coordinator.data."""
        return self.image()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 3 Molly & Cody Smart Feeder image entities from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[MollyCodyVerificationImageEntity] = [
        MollyCodyVerificationImageEntity(coordinator, entry, desc)
        for desc in IMAGE_DESCRIPTIONS
    ]
    async_add_entities(entities)
