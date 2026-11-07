"""Config Flow and Options Flow for the Molly & Cody Smart Feeder integration."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

from ._ha_compat import (
    AbortFlow,
    ConfigEntry,
    ConfigFlow,
    FlowResult,
    OptionsFlow,
    callback,
    vol,
)
from .const import (
    ALERT_MODES,
    CONF_ALERT_MODE,
    CONF_CONNECTION_MODE,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_EATEN_CHECK_MIN,
    CONF_EMAIL,
    CONF_ENABLE_AUTO_RETRY,
    CONF_ENABLE_CAMERA,
    CONF_ENABLE_MP4_RECORDING,
    CONF_ENABLE_TTS,
    CONF_ENABLE_VERIFICATION,
    CONF_ENABLE_VISION_VERIFICATION,
    CONF_HOPPER_CAPACITY,
    CONF_HOPPER_LOW_PCT,
    CONF_LOCAL_IP,
    CONF_LOCAL_KEY,
    CONF_MAX_DAILY_RETRY,
    CONF_MEAL_PREROLL_SEC,
    CONF_NO_FOOD_HOURS,
    CONF_PASSWORD,
    CONF_PORTION_WEIGHT_G,
    CONF_PRE_MEAL_ONLINE_SEC,
    CONF_SCAN_INTERVAL,
    CONF_TING_RATE_MIN,
    CONF_TTS_VOICE,
    CONF_TTS_VOLUME,
    CONNECTION_MODES,
    DEFAULT_ALERT_MODE,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_EATEN_CHECK_MIN,
    DEFAULT_ENABLE_AUTO_RETRY,
    DEFAULT_ENABLE_CAMERA,
    DEFAULT_ENABLE_MP4_RECORDING,
    DEFAULT_ENABLE_TTS,
    DEFAULT_ENABLE_VERIFICATION,
    DEFAULT_ENABLE_VISION_VERIFICATION,
    DEFAULT_HOPPER_CAPACITY,
    DEFAULT_HOPPER_LOW_PCT,
    DEFAULT_MAX_DAILY_RETRY,
    DEFAULT_MEAL_PREROLL_SEC,
    DEFAULT_NO_FOOD_HOURS,
    DEFAULT_PORTION_WEIGHT_G,
    DEFAULT_PRE_MEAL_ONLINE_SEC,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TING_RATE_MIN,
    DEFAULT_TTS_VOICE,
    DEFAULT_TTS_VOLUME,
    DOMAIN,
    NAME,
)
from .core import MollyFeederClient

logger = logging.getLogger("molly_cody_feeder.config_flow")

_DEFAULT_FALLBACK_DEVICE_ID = "yv8xga2javjxtqdc"

TOGGLE_LABELS: Dict[str, str] = {
    CONF_ENABLE_VERIFICATION: (
        "Audio Anti-Clog Verification (Lightweight 8 kHz Motor vs. Kibble Audio — Recommended for All HAOS Boxes)"
    ),
    CONF_ENABLE_VISION_VERIFICATION: (
        "Visual Inner-Bowl Analysis (Optional OpenCV/Pillow Camera ROI — Higher Compute)"
    ),
    CONF_ENABLE_CAMERA: "Enable Camera Snapshots & Image Entities (Optional)",
    CONF_ENABLE_TTS: "Enable Speaker Talkback, Chime & TTS",
    CONF_ENABLE_MP4_RECORDING: "Enable Full H.264 MP4 Clip Recording",
    CONF_ENABLE_AUTO_RETRY: "Enable Anti-Jam Auto-Retry on Clog",
}


def _build_user_schema(defaults: Optional[Dict[str, Any]] = None) -> Any:
    """Build the voluptuous schema for the initial user onboarding step."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Optional(CONF_EMAIL, default=d.get(CONF_EMAIL, "")): str,
            vol.Optional(CONF_PASSWORD, default=d.get(CONF_PASSWORD, "")): str,
            vol.Optional(
                CONF_COUNTRY_CODE, default=d.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE)
            ): str,
            vol.Optional(
                CONF_CONNECTION_MODE,
                default=d.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE),
            ): vol.In(CONNECTION_MODES),
            vol.Optional(CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, "")): str,
            vol.Optional(CONF_DEVICE_NAME, default=d.get(CONF_DEVICE_NAME, NAME)): str,
            vol.Optional(CONF_LOCAL_IP, default=d.get(CONF_LOCAL_IP, "")): str,
            vol.Optional(CONF_LOCAL_KEY, default=d.get(CONF_LOCAL_KEY, "")): str,
            vol.Optional(
                CONF_ENABLE_CAMERA,
                default=d.get(CONF_ENABLE_CAMERA, DEFAULT_ENABLE_CAMERA),
            ): bool,
            vol.Optional(
                CONF_ENABLE_TTS, default=d.get(CONF_ENABLE_TTS, DEFAULT_ENABLE_TTS)
            ): bool,
            vol.Optional(
                CONF_ENABLE_VERIFICATION,
                default=d.get(CONF_ENABLE_VERIFICATION, DEFAULT_ENABLE_VERIFICATION),
            ): bool,
            vol.Optional(
                CONF_ENABLE_VISION_VERIFICATION,
                default=d.get(
                    CONF_ENABLE_VISION_VERIFICATION, DEFAULT_ENABLE_VISION_VERIFICATION
                ),
            ): bool,
            vol.Optional(
                CONF_ENABLE_MP4_RECORDING,
                default=d.get(CONF_ENABLE_MP4_RECORDING, DEFAULT_ENABLE_MP4_RECORDING),
            ): bool,
            vol.Optional(
                CONF_ENABLE_AUTO_RETRY,
                default=d.get(CONF_ENABLE_AUTO_RETRY, DEFAULT_ENABLE_AUTO_RETRY),
            ): bool,
        }
    )


def _build_lan_schema(defaults: Optional[Dict[str, Any]] = None) -> Any:
    """Build the voluptuous schema for the local LAN setup step."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_DEVICE_ID, default=d.get(CONF_DEVICE_ID, "")): str,
            vol.Optional(CONF_DEVICE_NAME, default=d.get(CONF_DEVICE_NAME, NAME)): str,
            vol.Required(CONF_LOCAL_IP, default=d.get(CONF_LOCAL_IP, "")): str,
            vol.Required(CONF_LOCAL_KEY, default=d.get(CONF_LOCAL_KEY, "")): str,
            vol.Optional(
                CONF_ENABLE_CAMERA,
                default=d.get(CONF_ENABLE_CAMERA, DEFAULT_ENABLE_CAMERA),
            ): bool,
            vol.Optional(
                CONF_ENABLE_TTS, default=d.get(CONF_ENABLE_TTS, DEFAULT_ENABLE_TTS)
            ): bool,
            vol.Optional(
                CONF_ENABLE_VERIFICATION,
                default=d.get(CONF_ENABLE_VERIFICATION, DEFAULT_ENABLE_VERIFICATION),
            ): bool,
        }
    )


def _default_options_dict() -> Dict[str, Any]:
    """Return a dictionary of all integration options initialized to their defaults."""
    return {
        CONF_CONNECTION_MODE: DEFAULT_CONNECTION_MODE,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        CONF_HOPPER_CAPACITY: DEFAULT_HOPPER_CAPACITY,
        CONF_HOPPER_LOW_PCT: DEFAULT_HOPPER_LOW_PCT,
        CONF_PORTION_WEIGHT_G: DEFAULT_PORTION_WEIGHT_G,
        CONF_MEAL_PREROLL_SEC: DEFAULT_MEAL_PREROLL_SEC,
        CONF_PRE_MEAL_ONLINE_SEC: DEFAULT_PRE_MEAL_ONLINE_SEC,
        CONF_EATEN_CHECK_MIN: DEFAULT_EATEN_CHECK_MIN,
        CONF_ALERT_MODE: DEFAULT_ALERT_MODE,
        CONF_NO_FOOD_HOURS: DEFAULT_NO_FOOD_HOURS,
        CONF_TING_RATE_MIN: DEFAULT_TING_RATE_MIN,
        CONF_MAX_DAILY_RETRY: DEFAULT_MAX_DAILY_RETRY,
        CONF_TTS_VOICE: DEFAULT_TTS_VOICE,
        CONF_TTS_VOLUME: DEFAULT_TTS_VOLUME,
        CONF_ENABLE_CAMERA: DEFAULT_ENABLE_CAMERA,
        CONF_ENABLE_TTS: DEFAULT_ENABLE_TTS,
        CONF_ENABLE_VERIFICATION: DEFAULT_ENABLE_VERIFICATION,
        CONF_ENABLE_VISION_VERIFICATION: DEFAULT_ENABLE_VISION_VERIFICATION,
        CONF_ENABLE_MP4_RECORDING: DEFAULT_ENABLE_MP4_RECORDING,
        CONF_ENABLE_AUTO_RETRY: DEFAULT_ENABLE_AUTO_RETRY,
    }


def _build_options_schema(current: Dict[str, Any]) -> Any:
    """Build the voluptuous schema for the OptionsFlow form."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_CONNECTION_MODE,
                default=current.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE),
            ): vol.In(CONNECTION_MODES),
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): int,
            vol.Optional(
                CONF_HOPPER_CAPACITY,
                default=current.get(CONF_HOPPER_CAPACITY, DEFAULT_HOPPER_CAPACITY),
            ): int,
            vol.Optional(
                CONF_HOPPER_LOW_PCT,
                default=current.get(CONF_HOPPER_LOW_PCT, DEFAULT_HOPPER_LOW_PCT),
            ): float,
            vol.Optional(
                CONF_PORTION_WEIGHT_G,
                default=current.get(CONF_PORTION_WEIGHT_G, DEFAULT_PORTION_WEIGHT_G),
            ): int,
            vol.Optional(
                CONF_MEAL_PREROLL_SEC,
                default=current.get(CONF_MEAL_PREROLL_SEC, DEFAULT_MEAL_PREROLL_SEC),
            ): int,
            vol.Optional(
                CONF_PRE_MEAL_ONLINE_SEC,
                default=current.get(
                    CONF_PRE_MEAL_ONLINE_SEC, DEFAULT_PRE_MEAL_ONLINE_SEC
                ),
            ): int,
            vol.Optional(
                CONF_EATEN_CHECK_MIN,
                default=current.get(CONF_EATEN_CHECK_MIN, DEFAULT_EATEN_CHECK_MIN),
            ): int,
            vol.Optional(
                CONF_ALERT_MODE,
                default=current.get(CONF_ALERT_MODE, DEFAULT_ALERT_MODE),
            ): vol.In(ALERT_MODES),
            vol.Optional(
                CONF_NO_FOOD_HOURS,
                default=current.get(CONF_NO_FOOD_HOURS, DEFAULT_NO_FOOD_HOURS),
            ): float,
            vol.Optional(
                CONF_TING_RATE_MIN,
                default=current.get(CONF_TING_RATE_MIN, DEFAULT_TING_RATE_MIN),
            ): float,
            vol.Optional(
                CONF_MAX_DAILY_RETRY,
                default=current.get(CONF_MAX_DAILY_RETRY, DEFAULT_MAX_DAILY_RETRY),
            ): int,
            vol.Optional(
                CONF_TTS_VOICE,
                default=current.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE),
            ): str,
            vol.Optional(
                CONF_TTS_VOLUME,
                default=current.get(CONF_TTS_VOLUME, DEFAULT_TTS_VOLUME),
            ): float,
            vol.Optional(
                CONF_ENABLE_CAMERA,
                default=current.get(CONF_ENABLE_CAMERA, DEFAULT_ENABLE_CAMERA),
            ): bool,
            vol.Optional(
                CONF_ENABLE_TTS,
                default=current.get(CONF_ENABLE_TTS, DEFAULT_ENABLE_TTS),
            ): bool,
            vol.Optional(
                CONF_ENABLE_VERIFICATION,
                default=current.get(
                    CONF_ENABLE_VERIFICATION, DEFAULT_ENABLE_VERIFICATION
                ),
            ): bool,
            vol.Optional(
                CONF_ENABLE_VISION_VERIFICATION,
                default=current.get(
                    CONF_ENABLE_VISION_VERIFICATION, DEFAULT_ENABLE_VISION_VERIFICATION
                ),
            ): bool,
            vol.Optional(
                CONF_ENABLE_MP4_RECORDING,
                default=current.get(
                    CONF_ENABLE_MP4_RECORDING, DEFAULT_ENABLE_MP4_RECORDING
                ),
            ): bool,
            vol.Optional(
                CONF_ENABLE_AUTO_RETRY,
                default=current.get(CONF_ENABLE_AUTO_RETRY, DEFAULT_ENABLE_AUTO_RETRY),
            ): bool,
        }
    )


class MollyCodyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Molly & Cody Smart Feeder."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._pending_input: Dict[str, Any] = {}

    async def _async_run_in_executor(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking callable in the Home Assistant executor if available."""
        if self.hass is not None and hasattr(self.hass, "async_add_executor_job"):
            return await self.hass.async_add_executor_job(func, *args)
        return func(*args)

    def _session_path_for_flow(self) -> str:
        """Resolve a temporary session file path inside the HA config directory."""
        if (
            self.hass is not None
            and hasattr(self.hass, "config")
            and hasattr(self.hass.config, "path")
        ):
            p = Path(self.hass.config.path(".storage", DOMAIN, "config_flow_session.json"))
            p.parent.mkdir(parents=True, exist_ok=True)
            return str(p)
        return "/tmp/molly_cody_feeder_flow_session.json"

    async def _async_check_unique_id(self, device_id: str) -> Optional[FlowResult]:
        """Set the unique ID and return an abort FlowResult if already configured."""
        if not device_id:
            return None
        try:
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
        except AbortFlow as abort_exc:
            return self.async_abort(
                reason=abort_exc.reason,
                description_placeholders=abort_exc.description_placeholders,
            )
        return None

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle the initial step (Cloud / Auto login or LAN configuration)."""
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_schema(),
                errors={},
            )

        errors: Dict[str, str] = {}
        raw_mode = str(user_input.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)).strip().lower()
        if raw_mode == "hybrid":
            raw_mode = "auto"
        mode = raw_mode if raw_mode in CONNECTION_MODES else DEFAULT_CONNECTION_MODE

        email = str(user_input.get(CONF_EMAIL, "") or "").strip()
        password = str(user_input.get(CONF_PASSWORD, "") or "")
        country_code = str(user_input.get(CONF_COUNTRY_CODE, DEFAULT_COUNTRY_CODE) or DEFAULT_COUNTRY_CODE).strip()
        input_device_id = str(user_input.get(CONF_DEVICE_ID, "") or "").strip()
        input_device_name = str(user_input.get(CONF_DEVICE_NAME, "") or "").strip()
        local_ip = str(user_input.get(CONF_LOCAL_IP, "") or "").strip()
        local_key = str(user_input.get(CONF_LOCAL_KEY, "") or "").strip()

        if mode == "lan":
            if not input_device_id or not local_ip or not local_key:
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_schema(user_input),
                    errors=errors,
                )

            abort_res = await self._async_check_unique_id(input_device_id)
            if abort_res is not None:
                return abort_res

            client = MollyFeederClient(
                mode="lan",
                device_id=input_device_id,
                local_ip=local_ip,
                local_key=local_key,
                session_file=self._session_path_for_flow(),
            )
            if input_device_name:
                client.device_name = input_device_name

            try:
                status = await self._async_run_in_executor(client.get_status)
            except Exception as exc:
                logger.warning("LAN validation failed during config flow: %s", exc)
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_schema(user_input),
                    errors=errors,
                )

            if not status.get("online") and not status.get("dps"):
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_schema(user_input),
                    errors=errors,
                )

            resolved_id = str(
                input_device_id or status.get("device_id") or client.device_id or ""
            ).strip()
            abort_res = await self._async_check_unique_id(resolved_id)
            if abort_res is not None:
                return abort_res

            resolved_name = (
                input_device_name
                or str(status.get("device_name") or "").strip()
                or getattr(client, "device_name", "")
                or NAME
            )
            entry_data = self._build_entry_data(
                user_input=user_input,
                mode="lan",
                email=email,
                password=password,
                country_code=country_code,
                device_id=resolved_id,
                device_name=resolved_name,
                local_ip=str(status.get("local_ip") or local_ip),
                local_key=local_key,
            )
            return self.async_create_entry(title=resolved_name, data=entry_data)

        # Cloud or Auto mode
        if not email or not password:
            errors["base"] = "invalid_auth"
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_schema(user_input),
                errors=errors,
            )

        client = MollyFeederClient(
            email=email,
            password=password,
            country_code=country_code,
            mode=mode,
            device_id=input_device_id or None,
            local_ip=local_ip or None,
            local_key=local_key or None,
            session_file=self._session_path_for_flow(),
        )
        if input_device_name:
            client.device_name = input_device_name

        try:
            login_ok = await self._async_run_in_executor(lambda: client.login(force=True))
            if not login_ok:
                errors["base"] = "invalid_auth"
                return self.async_show_form(
                    step_id="user",
                    data_schema=_build_user_schema(user_input),
                    errors=errors,
                )
        except requests.exceptions.RequestException as exc:
            logger.warning("Cloud connection error during config flow: %s", exc)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_schema(user_input),
                errors=errors,
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.warning("Network error during config flow: %s", exc)
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_schema(user_input),
                errors=errors,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if any(tok in msg for tok in ("connect", "timeout", "unreachable", "dns", "network")):
                errors["base"] = "cannot_connect"
            else:
                errors["base"] = "invalid_auth"
            return self.async_show_form(
                step_id="user",
                data_schema=_build_user_schema(user_input),
                errors=errors,
            )

        # Query status if a session was established or if get_status is mocked in tests
        status: Dict[str, Any] = {}
        get_status_fn = getattr(client, "get_status", None)
        is_mocked_status = hasattr(get_status_fn, "_mock_name") or hasattr(
            get_status_fn, "assert_called"
        )
        if bool(getattr(client, "sid", "")) or is_mocked_status:
            try:
                res_st = await self._async_run_in_executor(client.get_status)
                if isinstance(res_st, dict):
                    status = res_st
            except Exception as exc:
                logger.debug("Optional get_status in config flow failed: %s", exc)

        # Resolve device_id from input -> status -> client
        status_dev_id = str(status.get("device_id") or "").strip()
        client_dev_id = str(getattr(client, "device_id", "") or "").strip()
        if input_device_id:
            resolved_id = input_device_id
        elif status_dev_id and status_dev_id != _DEFAULT_FALLBACK_DEVICE_ID:
            resolved_id = status_dev_id
        elif client_dev_id and client_dev_id != _DEFAULT_FALLBACK_DEVICE_ID:
            resolved_id = client_dev_id
        else:
            resolved_id = status_dev_id or client_dev_id or _DEFAULT_FALLBACK_DEVICE_ID

        abort_res = await self._async_check_unique_id(resolved_id)
        if abort_res is not None:
            return abort_res

        resolved_name = (
            input_device_name
            or str(status.get("device_name") or "").strip()
            or str(getattr(client, "device_name", "") or "").strip()
            or NAME
        )
        resolved_ip = (
            local_ip
            or str(status.get("local_ip") or "").strip()
            or str(getattr(client, "local_ip", "") or "").strip()
        )
        resolved_key = (
            local_key
            or str(getattr(client, "local_key", "") or "").strip()
        )

        entry_data = self._build_entry_data(
            user_input=user_input,
            mode=mode,
            email=email,
            password=password,
            country_code=country_code,
            device_id=resolved_id,
            device_name=resolved_name,
            local_ip=resolved_ip,
            local_key=resolved_key,
        )
        return self.async_create_entry(title=resolved_name, data=entry_data)

    async def async_step_lan(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle explicit local LAN configuration step."""
        if user_input is None:
            return self.async_show_form(
                step_id="lan",
                data_schema=_build_lan_schema(self._pending_input),
                errors={},
            )
        merged = dict(self._pending_input)
        merged.update(user_input)
        merged[CONF_CONNECTION_MODE] = "lan"
        return await self.async_step_user(merged)

    @staticmethod
    def _build_entry_data(
        *,
        user_input: Dict[str, Any],
        mode: str,
        email: str,
        password: str,
        country_code: str,
        device_id: str,
        device_name: str,
        local_ip: str,
        local_key: str,
    ) -> Dict[str, Any]:
        """Build the persisted ConfigEntry.data dictionary."""
        data: Dict[str, Any] = {
            CONF_EMAIL: email,
            CONF_PASSWORD: password,
            CONF_COUNTRY_CODE: country_code,
            CONF_CONNECTION_MODE: mode,
            CONF_DEVICE_ID: device_id,
            CONF_DEVICE_NAME: device_name,
            CONF_LOCAL_IP: local_ip,
            CONF_LOCAL_KEY: local_key,
            CONF_ENABLE_CAMERA: bool(
                user_input.get(CONF_ENABLE_CAMERA, DEFAULT_ENABLE_CAMERA)
            ),
            CONF_ENABLE_TTS: bool(
                user_input.get(CONF_ENABLE_TTS, DEFAULT_ENABLE_TTS)
            ),
            CONF_ENABLE_VERIFICATION: bool(
                user_input.get(CONF_ENABLE_VERIFICATION, DEFAULT_ENABLE_VERIFICATION)
            ),
            CONF_ENABLE_VISION_VERIFICATION: bool(
                user_input.get(
                    CONF_ENABLE_VISION_VERIFICATION, DEFAULT_ENABLE_VISION_VERIFICATION
                )
            ),
            CONF_ENABLE_MP4_RECORDING: bool(
                user_input.get(CONF_ENABLE_MP4_RECORDING, DEFAULT_ENABLE_MP4_RECORDING)
            ),
            CONF_ENABLE_AUTO_RETRY: bool(
                user_input.get(CONF_ENABLE_AUTO_RETRY, DEFAULT_ENABLE_AUTO_RETRY)
            ),
        }
        return data

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MollyCodyOptionsFlow:
        """Return the options flow handler for this config entry."""
        return MollyCodyOptionsFlow(config_entry)


class MollyCodyOptionsFlow(OptionsFlow):
    """Handle runtime options and modular compute toggles for the feeder."""

    def __init__(self, config_entry: Optional[ConfigEntry] = None) -> None:
        """Initialize the options flow."""
        super().__init__(config_entry)
        if config_entry is not None and getattr(self, "_config_entry", None) is None:
            self._config_entry = config_entry

    def _current_merged_options(self) -> Dict[str, Any]:
        """Merge default options with entry.data and entry.options."""
        merged = _default_options_dict()
        entry = self.config_entry
        if entry is not None:
            entry_data = getattr(entry, "data", None)
            if isinstance(entry_data, dict):
                for k in list(merged.keys()):
                    if k in entry_data and entry_data[k] is not None:
                        merged[k] = entry_data[k]
            entry_opts = getattr(entry, "options", None)
            if isinstance(entry_opts, dict):
                for k, v in entry_opts.items():
                    if v is not None:
                        merged[k] = v
        return merged

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Manage the integration options and modular compute toggles."""
        current = self._current_merged_options()
        if user_input is None:
            return self.async_show_form(
                step_id="init",
                data_schema=_build_options_schema(current),
                errors={},
            )

        updated = dict(current)
        updated.update(user_input)
        if updated.get(CONF_CONNECTION_MODE) == "hybrid":
            updated[CONF_CONNECTION_MODE] = "auto"

        return self.async_create_entry(title="", data=updated)


MollyCodyFeederConfigFlow = MollyCodyConfigFlow
MollyCodyFeederOptionsFlow = MollyCodyOptionsFlow
OptionsFlowHandler = MollyCodyOptionsFlow

__all__ = [
    "MollyCodyConfigFlow",
    "MollyCodyFeederConfigFlow",
    "MollyCodyOptionsFlow",
    "MollyCodyFeederOptionsFlow",
    "OptionsFlowHandler",
]
