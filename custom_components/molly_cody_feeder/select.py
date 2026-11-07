"""Select platform for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from ._ha_compat import (
    ConfigEntry,
    CoordinatorEntity,
    DeviceInfo,
    EntityCategory,
    HomeAssistant,
    SelectEntity,
    SelectEntityDescription,
)
from .const import (
    ALERT_MODES,
    CONF_ALERT_MODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONNECTION_MODES,
    DEFAULT_ALERT_MODE,
    DEFAULT_CONNECTION_MODE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    MOTION_SENSITIVITY_LABELS_TO_DP,
    MOTION_SENSITIVITY_MODES,
    NAME,
    NIGHTVISION_LABELS_TO_DP,
    NIGHTVISION_MODES,
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


class MollyCodyConnectionModeSelect(CoordinatorEntity[Dict[str, Any]], SelectEntity):
    """Select entity for choosing transport mode (`auto`, `cloud`, `lan`)."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        """Initialize the connection mode select entity."""
        super().__init__(coordinator)
        key = "connection_mode"
        self.entity_description = SelectEntityDescription(
            key=key,
            name="Connection Mode",
            icon="mdi:connection",
            entity_category=EntityCategory.CONFIG,
            options=list(CONNECTION_MODES),
            translation_key=key,
        )
        self._key = key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = self.entity_description.name
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = list(CONNECTION_MODES)
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the select key."""
        return self._key

    @property
    def current_option(self) -> str:
        """Return the currently configured connection mode."""
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict):
            mode = str(data.get("configured_mode", DEFAULT_CONNECTION_MODE)).lower()
            if mode in self._attr_options:
                return mode
        return DEFAULT_CONNECTION_MODE

    async def async_select_option(self, option: str) -> None:
        """Change the active connection mode via coordinator."""
        normalized = str(option).strip().lower()
        await self.coordinator.async_set_connection_mode(normalized)
        self.async_write_ha_state()


class MollyCodyDpSelect(CoordinatorEntity[Dict[str, Any]], SelectEntity):
    """Select entity mapping a Tuya enum DP (DP 108 Night Vision, DP 106 Motion Sensitivity)."""

    def __init__(
        self,
        coordinator: Any,
        entry: ConfigEntry,
        key: str,
        name: str,
        dp_id: int,
        icon: str,
        dp_to_label: Dict[str, str],
        label_to_dp: Dict[str, str],
        default_dp: str,
    ) -> None:
        """Initialize the hardware DP select entity."""
        super().__init__(coordinator)
        self._key = key
        self._dp_id = int(dp_id)
        self._dp_to_label = dict(dp_to_label)
        self._label_to_dp = dict(label_to_dp)
        self._default_dp = str(default_dp)
        opts = list(label_to_dp.keys())
        self.entity_description = SelectEntityDescription(
            key=key,
            name=name,
            icon=icon,
            entity_category=EntityCategory.CONFIG,
            options=opts,
            translation_key=key,
        )
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = opts
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the select key."""
        return self._key

    @property
    def current_option(self) -> str:
        """Return the current human-readable option for the DP."""
        data = getattr(self.coordinator, "data", None)
        raw_val = self._default_dp
        if isinstance(data, dict) and isinstance(data.get("dps"), dict):
            dps = data["dps"]
            raw_val = str(dps.get(str(self._dp_id), dps.get(self._dp_id, self._default_dp)))
        if raw_val in self._dp_to_label:
            return self._dp_to_label[raw_val]
        lower_val = raw_val.strip().lower()
        if lower_val in self._label_to_dp:
            return lower_val
        return self._dp_to_label.get(self._default_dp, self._attr_options[0])

    async def async_select_option(self, option: str) -> None:
        """Select a new option and write the mapped DP value to the feeder."""
        norm = str(option).strip().lower()
        if norm in self._label_to_dp:
            dp_val = self._label_to_dp[norm]
        elif norm in self._dp_to_label:
            dp_val = norm
        else:
            dp_val = self._default_dp
        await self.coordinator.async_set_dp(self._dp_id, dp_val)
        self.async_write_ha_state()


class MollyCodyAlertModeSelect(CoordinatorEntity[Dict[str, Any]], SelectEntity):
    """Select entity for choosing the clog/starvation alert mode (`every_miss` vs `no_food_hours`)."""

    def __init__(self, coordinator: Any, entry: ConfigEntry) -> None:
        """Initialize the alert mode select entity."""
        super().__init__(coordinator)
        key = "alert_mode"
        self.entity_description = SelectEntityDescription(
            key=key,
            name="Alert Mode",
            icon="mdi:bell-cog-outline",
            entity_category=EntityCategory.CONFIG,
            options=list(ALERT_MODES),
            translation_key=key,
        )
        self._key = key
        device_id, device_info = _build_device_info(coordinator, entry)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = self.entity_description.name
        self._attr_device_info = device_info
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = list(ALERT_MODES)
        self._attr_translation_key = key

    @property
    def key(self) -> str:
        """Return the select key."""
        return self._key

    @property
    def current_option(self) -> str:
        """Return the active alert mode."""
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict) and isinstance(data.get("options"), dict):
            val = data["options"].get(CONF_ALERT_MODE)
            if val in self._attr_options:
                return str(val)
        opts = getattr(self.coordinator, "options", None)
        if isinstance(opts, dict) and opts.get(CONF_ALERT_MODE) in self._attr_options:
            return str(opts[CONF_ALERT_MODE])
        return DEFAULT_ALERT_MODE

    async def async_select_option(self, option: str) -> None:
        """Update the alert mode option on the coordinator."""
        norm = str(option).strip()
        if hasattr(self.coordinator, "async_set_option"):
            await self.coordinator.async_set_option(CONF_ALERT_MODE, norm)
        if hasattr(self.coordinator, "options") and isinstance(self.coordinator.options, dict):
            self.coordinator.options[CONF_ALERT_MODE] = norm
        if isinstance(getattr(self.coordinator, "data", None), dict):
            self.coordinator.data.setdefault("options", {})[CONF_ALERT_MODE] = norm
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[[List[Any]], None],
) -> None:
    """Set up the 4 Molly & Cody Smart Feeder select entities from a config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    entities: List[SelectEntity] = [
        # 1. Connection Mode (`auto`, `cloud`, `lan`)
        MollyCodyConnectionModeSelect(coordinator, entry),
        # 2. Night Vision (DP 108: `"0"`->`auto`, `"1"`->`off`, `"2"`->`on`)
        MollyCodyDpSelect(
            coordinator,
            entry,
            key="nightvision",
            name="Night Vision",
            dp_id=108,
            icon="mdi:weather-night",
            dp_to_label=NIGHTVISION_MODES,
            label_to_dp=NIGHTVISION_LABELS_TO_DP,
            default_dp="0",
        ),
        # 3. Motion Sensitivity (DP 106: `"0"`->`low`, `"1"`->`medium`, `"2"`->`high`)
        MollyCodyDpSelect(
            coordinator,
            entry,
            key="motion_sensitivity",
            name="Motion Sensitivity",
            dp_id=106,
            icon="mdi:run-fast",
            dp_to_label=MOTION_SENSITIVITY_MODES,
            label_to_dp=MOTION_SENSITIVITY_LABELS_TO_DP,
            default_dp="1",
        ),
        # 4. Alert Mode (`every_miss`, `no_food_hours`)
        MollyCodyAlertModeSelect(coordinator, entry),
    ]
    async_add_entities(entities)
