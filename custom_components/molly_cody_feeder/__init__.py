"""Molly & Cody Smart Feeder Home Assistant Custom Integration (`molly_cody_feeder`)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ._ha_compat import ConfigEntry, HomeAssistant, ServiceCall, SupportsResponse
from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_CAPTURE_SNAPSHOT,
    SERVICE_CHECK_BOWL,
    SERVICE_CLEAR_CLOG_ALERT,
    SERVICE_FEED,
    SERVICE_PLAY_AUDIO,
    SERVICE_PLAY_CHIME,
    SERVICE_PLAY_TTS,
    SERVICE_REFILL_HOPPER,
    SERVICE_RUN_ONLINE_CHECK,
    SERVICE_SET_DP,
    SERVICE_SET_SCHEDULE_RAW,
    SERVICE_SET_SCHEDULE_SLOT,
    SERVICE_SYNC_SCHEDULES,
    SERVICE_VERIFY_FEED,
    VERSION,
)
from .coordinator import MollyCodyDataUpdateCoordinator

__all__ = [
    "DOMAIN",
    "PLATFORMS",
    "VERSION",
    "MollyCodyDataUpdateCoordinator",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
]

logger = logging.getLogger("custom_components.molly_cody_feeder")


def _get_coordinators(hass: HomeAssistant, call_data: Optional[Dict[str, Any]] = None) -> List[MollyCodyDataUpdateCoordinator]:
    """Return active coordinator instances matching optional `entry_id` or `device_id` filter."""
    domain_data = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    target_entry_id = (call_data or {}).get("entry_id")
    target_device_id = (call_data or {}).get("device_id")

    coordinators: List[MollyCodyDataUpdateCoordinator] = []
    for eid, item in domain_data.items():
        if target_entry_id and str(eid) != str(target_entry_id):
            continue
        coord = item.get("coordinator") if isinstance(item, dict) else getattr(item, "coordinator", item)
        if isinstance(coord, MollyCodyDataUpdateCoordinator):
            if target_device_id:
                dev_id = (coord.data or {}).get("device_id") or getattr(coord.client, "device_id", "")
                if str(dev_id) != str(target_device_id):
                    continue
            coordinators.append(coord)
    return coordinators


def _extract_call_data(call: Any) -> Dict[str, Any]:
    """Extract service data dict from either a `ServiceCall` object or a raw dict."""
    if isinstance(call, dict):
        return dict(call)
    data = getattr(call, "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _async_register_services(hass: HomeAssistant) -> None:
    """Register all 14 custom `molly_cody_feeder.*` domain services on `hass.services`."""
    if not hasattr(hass, "services"):
        return

    async def _handle_feed(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        portions = data.get("portions")
        verify = bool(data.get("verify", False))
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_feed(
                portions=int(portions) if portions is not None else None,
                verify=verify,
            )
        return last_res

    async def _handle_verify_feed(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        portions = data.get("portions")
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_verify_feed(
                portions=int(portions) if portions is not None else None,
                trigger="manual_dispense_verify",
            )
        return last_res

    async def _handle_check_bowl(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_check_bowl()
        return last_res

    async def _handle_set_schedule_slot(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        slot = int(data.get("slot", 1))
        hour = data.get("hour")
        minute = data.get("minute")
        time_str = data.get("time")
        if time_str and (hour is None or minute is None):
            parts = [int(x) for x in str(time_str).strip().split(":")]
            hour = parts[0]
            minute = parts[1] if len(parts) > 1 else 0
        portions = data.get("portions")
        days = data.get("days")
        days_mask = data.get("days_mask")
        enabled = data.get("enabled")
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_set_schedule_slot(
                slot=slot,
                hour=int(hour) if hour is not None else None,
                minute=int(minute) if minute is not None else None,
                portions=int(portions) if portions is not None else None,
                days=days,
                days_mask=int(days_mask) if days_mask is not None else None,
                enabled=bool(enabled) if enabled is not None else None,
            )
        return last_res

    async def _handle_set_schedule_raw(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        raw = str(data.get("schedule_raw") or data.get("raw_schedule") or data.get("raw") or "")
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_set_schedule_raw(raw)
        return last_res

    async def _handle_sync_schedules(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_sync_schedules()
        return last_res

    async def _handle_refill_hopper(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        cap = data.get("capacity_portions")
        if cap is None:
            cap = data.get("portions", data.get("capacity"))
        note = str(data.get("note", ""))
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_refill_hopper(
                capacity_portions=int(cap) if cap is not None else None,
                note=note,
            )
        return last_res

    async def _handle_capture_snapshot(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_capture_snapshot()
        return last_res

    async def _handle_play_tts(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        msg = str(data.get("message") or data.get("text") or "")
        voice = data.get("voice")
        volume = data.get("volume")
        repeat = int(data.get("repeat", 1) or 1)
        prepend_chime = bool(data.get("prepend_chime", False))
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_play_tts(
                message=msg,
                voice=str(voice) if voice is not None else None,
                volume=float(volume) if volume is not None else None,
                repeat=repeat,
                prepend_chime=prepend_chime,
            )
        return last_res

    async def _handle_play_audio(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        audio_path = str(data.get("audio_path") or data.get("path") or data.get("file") or "")
        volume = data.get("volume")
        prepend_chime = bool(data.get("prepend_chime", False))
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_play_audio(
                audio_path=audio_path,
                volume=float(volume) if volume is not None else None,
                prepend_chime=prepend_chime,
            )
        return last_res

    async def _handle_play_chime(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        volume = data.get("volume")
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_play_chime(
                volume=float(volume) if volume is not None else None,
            )
        return last_res

    async def _handle_set_dp(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        dp_id = data.get("dp_id", data.get("dp"))
        value = data.get("value")
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_set_dp(dp_id, value)
        return last_res

    async def _handle_run_online_check(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        meal_time = str(data.get("meal_time", ""))
        last_res = None
        for coord in _get_coordinators(hass, data):
            last_res = await coord.async_run_online_check(meal_time=meal_time)
        return last_res

    async def _handle_clear_clog_alert(call: ServiceCall) -> Any:
        data = _extract_call_data(call)
        for coord in _get_coordinators(hass, data):
            await coord.async_clear_clog_alert()
        return None

    service_map = (
        (SERVICE_FEED, _handle_feed),
        (SERVICE_VERIFY_FEED, _handle_verify_feed),
        (SERVICE_CHECK_BOWL, _handle_check_bowl),
        (SERVICE_SET_SCHEDULE_SLOT, _handle_set_schedule_slot),
        (SERVICE_SET_SCHEDULE_RAW, _handle_set_schedule_raw),
        (SERVICE_SYNC_SCHEDULES, _handle_sync_schedules),
        (SERVICE_REFILL_HOPPER, _handle_refill_hopper),
        (SERVICE_CAPTURE_SNAPSHOT, _handle_capture_snapshot),
        (SERVICE_PLAY_TTS, _handle_play_tts),
        (SERVICE_PLAY_AUDIO, _handle_play_audio),
        (SERVICE_PLAY_CHIME, _handle_play_chime),
        (SERVICE_SET_DP, _handle_set_dp),
        (SERVICE_RUN_ONLINE_CHECK, _handle_run_online_check),
        (SERVICE_CLEAR_CLOG_ALERT, _handle_clear_clog_alert),
    )

    for svc_name, handler in service_map:
        try:
            hass.services.async_register(
                DOMAIN,
                svc_name,
                handler,
                supports_response=SupportsResponse.OPTIONAL,
            )
        except TypeError:
            hass.services.async_register(DOMAIN, svc_name, handler)


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Propagate ConfigEntry options updates to the active coordinator at runtime."""
    domain_data = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    stored = domain_data.get(entry.entry_id)
    coord = stored.get("coordinator") if isinstance(stored, dict) else getattr(stored, "coordinator", stored)
    if isinstance(coord, MollyCodyDataUpdateCoordinator):
        for k, v in dict(getattr(entry, "options", None) or {}).items():
            await coord.async_set_option(k, v)


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """Set up the Molly & Cody Smart Feeder component namespace."""
    hass.data.setdefault(DOMAIN, {})
    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Molly & Cody Smart Feeder config entry and forward all 9 entity platforms."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = MollyCodyDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "client": coordinator.client,
        "db": coordinator.db,
        "runtime_config": coordinator.runtime_config,
    }
    entry.runtime_data = coordinator

    if hasattr(entry, "async_on_unload") and hasattr(entry, "add_update_listener"):
        entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _async_register_services(hass)

    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_forward_entry_setups"):
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Molly & Cody Smart Feeder config entry and clean up `hass.data`."""
    unload_ok = True
    if hasattr(hass, "config_entries") and hasattr(hass.config_entries, "async_unload_platforms"):
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    domain_data = hass.data.get(DOMAIN, {}) if hasattr(hass, "data") and isinstance(hass.data, dict) else {}
    stored = domain_data.pop(entry.entry_id, None)
    coord = stored.get("coordinator") if isinstance(stored, dict) else getattr(stored, "coordinator", stored)
    if isinstance(coord, MollyCodyDataUpdateCoordinator):
        await coord.async_shutdown()

    return bool(unload_ok)
