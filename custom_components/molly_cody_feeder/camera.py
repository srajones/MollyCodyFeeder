"""Camera platform (`MollyCodyBowlCamera`) for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ._ha_compat import (
    Camera,
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityDescription,
    HomeAssistant,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CAMERA,
    DEFAULT_ENABLE_CAMERA,
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


class MollyCodyBowlCamera(CoordinatorEntity[Dict[str, Any]], Camera):
    """Camera entity capturing upright 720x1280 snapshots of the feeder bowl via Cloud `imm`."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        """Initialize the Molly & Cody bowl camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        key = "bowl_camera"
        self.entity_description = EntityDescription(
            key=key,
            name="Bowl Camera",
            icon="mdi:camera",
            translation_key=key,
        )
        self._key = key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = "Bowl Camera"
        self._attr_device_info = device_info
        self._attr_translation_key = key
        self.content_type = "image/jpeg"

    @property
    def key(self) -> str:
        """Return the camera key."""
        return self._key

    def _is_camera_enabled(self) -> bool:
        """Check whether `enable_camera` is enabled in coordinator data or options."""
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict) and isinstance(data.get("options"), dict):
            if CONF_ENABLE_CAMERA in data["options"]:
                return bool(data["options"][CONF_ENABLE_CAMERA])
        opts = getattr(self.coordinator, "options", None)
        if isinstance(opts, dict) and CONF_ENABLE_CAMERA in opts:
            return bool(opts[CONF_ENABLE_CAMERA])
        return DEFAULT_ENABLE_CAMERA

    def _is_busy_or_locked(self) -> bool:
        """Check whether the coordinator is currently busy or holding the `imm` session lock."""
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict) and isinstance(data.get("summary"), dict):
            if bool(data["summary"].get("busy", False)):
                return True
        lock = (
            getattr(self.coordinator, "session_lock", None)
            or getattr(self.coordinator, "_session_lock", None)
            or getattr(self.coordinator, "_lock", None)
        )
        if lock is not None and hasattr(lock, "locked") and callable(lock.locked):
            if bool(lock.locked()):
                return True
        return False

    @property
    def available(self) -> bool:
        """Return True if the camera is enabled and the coordinator is available."""
        if not self._is_camera_enabled():
            return False
        return super().available

    async def async_camera_image(
        self, width: Optional[int] = None, height: Optional[int] = None
    ) -> Optional[bytes]:
        """Return a JPEG snapshot of the bowl, respecting `enable_camera` and session lock."""
        data = getattr(self.coordinator, "data", None)
        cached = data.get("latest_snapshot_bytes") if isinstance(data, dict) else None

        # 1. Never initiate live capture when `enable_camera` is disabled
        if not self._is_camera_enabled():
            return cached

        # 2. Never interrupt an active feed verification session or locked `imm` session
        if self._is_busy_or_locked():
            return cached

        # 3. Capture a snapshot via the coordinator
        if hasattr(self.coordinator, "async_capture_snapshot"):
            snap = await self.coordinator.async_capture_snapshot()
            if snap is not None:
                return snap

        data = getattr(self.coordinator, "data", None)
        return data.get("latest_snapshot_bytes") if isinstance(data, dict) else cached


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the Molly & Cody Smart Feeder camera entity from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    async_add_entities([MollyCodyBowlCamera(coordinator, entry)])
