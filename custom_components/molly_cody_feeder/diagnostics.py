"""Diagnostics support (`async_get_config_entry_diagnostics`) for Molly & Cody Smart Feeder."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Set

from ._ha_compat import ConfigEntry, HomeAssistant, REDACTED, async_redact_data
from .const import DOMAIN

TO_REDACT: Set[str] = {
    "email",
    "molly_user",
    "password",
    "molly_pass",
    "passwd",
    "local_key",
    "localKey",
    "feeder_local_key",
    "camera_password",
    "gemini_api_key",
    "sid",
    "ecode",
    "uid",
    "token",
    "webhook_secret",
    "ha_webhook_url",
    "ha_tts_webhook_url",
    "discord_webhook_url",
    "telegram_bot_token",
    "telegram_chat_id",
    "ntfy_topic_url",
    "heartbeat_url",
}


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


def _collect_secret_values(entry: ConfigEntry, coordinator: Any) -> Set[str]:
    """Collect non-empty secret values from entry, client, and runtime_config for value-level scrubbing."""
    secrets: Set[str] = set()
    entry_data = getattr(entry, "data", None) or {}
    entry_options = getattr(entry, "options", None) or {}
    for src in (entry_data, entry_options):
        if isinstance(src, dict):
            for k, v in src.items():
                if str(k) in TO_REDACT and isinstance(v, str) and v.strip():
                    secrets.add(v.strip())

    client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
    if client is not None:
        for attr in ("email", "password", "local_key", "camera_password", "sid", "ecode", "uid"):
            val = getattr(client, attr, None)
            if isinstance(val, str) and val.strip():
                secrets.add(val.strip())

    rt_cfg = getattr(coordinator, "runtime_config", None) or getattr(coordinator, "cfg", None)
    if rt_cfg is not None:
        for attr in (
            "email",
            "password",
            "local_key",
            "camera_password",
            "gemini_api_key",
            "ha_webhook_url",
            "ha_tts_webhook_url",
            "discord_webhook_url",
            "telegram_bot_token",
            "telegram_chat_id",
            "ntfy_topic_url",
            "heartbeat_url",
        ):
            val = getattr(rt_cfg, attr, None)
            if isinstance(val, str) and val.strip():
                secrets.add(val.strip())

    return secrets


def _sanitize_for_json(obj: Any, secret_values: Set[str]) -> Any:
    """Recursively convert non-JSON types and scrub any residual secret values."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes:{len(obj)}>"
    if isinstance(obj, Path):
        return _sanitize_for_json(str(obj), secret_values)
    if isinstance(obj, str):
        if obj in secret_values:
            return REDACTED
        scrubbed = obj
        for sv in secret_values:
            if len(sv) >= 4 and sv in scrubbed:
                scrubbed = scrubbed.replace(sv, REDACTED)
        return scrubbed
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            k_str = str(k)
            if k_str in TO_REDACT:
                out[k_str] = REDACTED
            else:
                out[k_str] = _sanitize_for_json(v, secret_values)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_sanitize_for_json(item, secret_values) for item in obj]
    return str(obj)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Dict[str, Any]:
    """Return redacted diagnostics for a Molly & Cody Smart Feeder config entry."""
    coordinator = _resolve_coordinator(hass, entry)
    secret_values = _collect_secret_values(entry, coordinator)

    raw_entry: Dict[str, Any] = {
        "entry_id": getattr(entry, "entry_id", ""),
        "domain": getattr(entry, "domain", DOMAIN),
        "title": getattr(entry, "title", ""),
        "data": dict(getattr(entry, "data", None) or {}),
        "options": dict(getattr(entry, "options", None) or {}),
    }

    client = getattr(coordinator, "client", None) or getattr(coordinator, "molly", None)
    client_diag: Dict[str, Any] = {}
    if client is not None:
        client_diag = {
            "device_id": getattr(client, "device_id", ""),
            "device_name": getattr(client, "device_name", ""),
            "mode": getattr(client, "mode", ""),
            "local_ip": getattr(client, "local_ip", ""),
            "email": getattr(client, "email", ""),
            "password": getattr(client, "password", ""),
            "local_key": getattr(client, "local_key", ""),
            "sid": getattr(client, "sid", ""),
            "uid": getattr(client, "uid", ""),
            "ecode": getattr(client, "ecode", ""),
        }

    coord_data = getattr(coordinator, "data", None) if coordinator is not None else None
    coord_dict = dict(coord_data) if isinstance(coord_data, dict) else {}
    for byte_key in (
        "latest_snapshot_bytes",
        "latest_annotated_bytes",
        "latest_before_bytes",
        "latest_after_bytes",
    ):
        if byte_key in coord_dict:
            b = coord_dict[byte_key]
            coord_dict[byte_key] = len(b) if isinstance(b, (bytes, bytearray)) else None

    summary = dict(coord_dict.get("summary") or {})

    db_diag: Dict[str, Any] = {}
    db = getattr(coordinator, "db", None)
    if db is not None:
        try:
            db_diag["stats"] = db.get_stats()
            db_diag["learned_baselines"] = db.learned_baselines()
            cap = int(summary.get("hopper_capacity_portions") or 240)
            db_diag["hopper"] = db.hopper_status(cap)
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "entry": async_redact_data(raw_entry, TO_REDACT),
        "client": async_redact_data(client_diag, TO_REDACT),
        "status": async_redact_data(coord_dict, TO_REDACT),
        "summary": async_redact_data(summary, TO_REDACT),
        "database": async_redact_data(db_diag, TO_REDACT),
    }

    return _sanitize_for_json(payload, secret_values)
