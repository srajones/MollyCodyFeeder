"""Home Assistant compatibility layer (`_ha_compat.py`).

Uses native `homeassistant.*` classes when running inside Home Assistant Core, and provides
genuine, lightweight, spec-faithful Python implementations when running standalone or in
unit tests without `homeassistant` installed.
"""
from __future__ import annotations

import asyncio
import builtins
import dataclasses
import logging
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Generic, Iterable, List, Mapping, Optional, Tuple, TypeVar, Union

import custom_components.molly_cody_feeder.core.analysis.vision  # noqa: F401
import custom_components.molly_cody_feeder.core.env  # noqa: F401

T = TypeVar("T")

REDACTED = "**REDACTED**"


def async_redact_data(data: Any, to_redact: Iterable[Any]) -> Any:
    """Recursively redact sensitive keys in mappings (matches `homeassistant.components.diagnostics`)."""
    redact_set = set(to_redact)
    if isinstance(data, Mapping):
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if k in redact_set:
                out[k] = REDACTED
            else:
                out[k] = async_redact_data(v, redact_set)
        return out
    if isinstance(data, list):
        return [async_redact_data(item, redact_set) for item in data]
    if isinstance(data, tuple):
        return tuple(async_redact_data(item, redact_set) for item in data)
    return data


# ---------------------------------------------------------------------------
# Voluptuous (`vol`) lightweight shim when `voluptuous` is not installed
# ---------------------------------------------------------------------------
try:
    import voluptuous as vol  # type: ignore[import-not-found]
except ImportError:

    _UNSET = object()

    class _VolInvalid(ValueError):
        """Voluptuous validation error."""

    class _VolMultipleInvalid(_VolInvalid):
        """Voluptuous multiple validation error."""

    class _VolMarker:
        def __init__(self, key: Any, default: Any = _UNSET, description: Any = None) -> None:
            self.schema = key
            self.key = key
            if default is _UNSET:
                self.default = None
                self.has_default = False
            else:
                self.default = default if callable(default) else (lambda d=default: d)
                self.has_default = True
            self.description = description

        def __hash__(self) -> int:
            return hash(self.key)

        def __eq__(self, other: Any) -> bool:
            if isinstance(other, _VolMarker):
                return bool(self.key == other.key)
            return bool(self.key == other)

        def __repr__(self) -> str:
            return f"{self.__class__.__name__}({self.key!r})"

    class _VolRequired(_VolMarker):
        pass

    class _VolOptional(_VolMarker):
        pass

    def _run_validator(validator: Any, val: Any) -> Any:
        if isinstance(validator, type):
            if validator is int and isinstance(val, bool):
                raise _VolInvalid(f"Expected int, got bool ({val!r})")
            if validator is float and isinstance(val, int) and not isinstance(val, bool):
                return float(val)
            if not isinstance(val, validator):
                raise _VolInvalid(f"Expected {validator.__name__}, got {type(val).__name__}")
            return val
        if callable(validator):
            try:
                return validator(val)
            except _VolInvalid:
                raise
            except (ValueError, TypeError) as exc:
                raise _VolInvalid(str(exc)) from exc
        return val

    class _VolSchema:
        def __init__(self, schema: Any, extra: Any = None) -> None:
            self.schema = schema
            self.extra = extra

        def __call__(self, data: Any) -> Any:
            if isinstance(self.schema, dict):
                if not isinstance(data, Mapping):
                    raise _VolInvalid(f"Expected a dictionary, got {type(data).__name__}")
                out = dict(data)
                for k, validator in self.schema.items():
                    key_name = k.key if isinstance(k, _VolMarker) else k
                    if key_name not in out:
                        if isinstance(k, _VolMarker) and getattr(k, "has_default", False) and k.default is not None:
                            out[key_name] = k.default()
                        elif isinstance(k, _VolRequired):
                            raise _VolInvalid(f"Required key {key_name!r} not provided")
                    if key_name in out:
                        out[key_name] = _run_validator(validator, out[key_name])
                return out
            return _run_validator(self.schema, data)

        def extend(self, extra_schema: Dict[Any, Any]) -> "_VolSchema":
            merged = dict(self.schema) if isinstance(self.schema, dict) else {}
            merged.update(extra_schema)
            return _VolSchema(merged, extra=self.extra)

    class _VolModule:
        ALLOW_EXTRA = 1
        PREVENT_EXTRA = 2
        Invalid = _VolInvalid
        MultipleInvalid = _VolMultipleInvalid
        Schema = _VolSchema
        Required = _VolRequired
        Optional = _VolOptional

        @staticmethod
        def In(container: Iterable[Any]) -> Callable[[Any], Any]:
            allowed = list(container)

            def _check(val: Any) -> Any:
                if val not in allowed:
                    raise _VolInvalid(f"Value {val!r} not in {allowed!r}")
                return val

            return _check

        @staticmethod
        def All(*validators: Any) -> Callable[[Any], Any]:
            def _run(val: Any) -> Any:
                cur = val
                for v in validators:
                    cur = _run_validator(v, cur)
                return cur

            return _run

        @staticmethod
        def Any(*validators: Any) -> Callable[[Any], Any]:
            def _run(val: Any) -> Any:
                last_err: Optional[Exception] = None
                for v in validators:
                    try:
                        return _run_validator(v, val)
                    except Exception as exc:
                        last_err = exc
                raise _VolInvalid(str(last_err or f"Value {val!r} did not match any validator"))

            return _run

        @staticmethod
        def Coerce(target_type: type) -> Callable[[Any], Any]:
            def _coerce(val: Any) -> Any:
                try:
                    if target_type is int and isinstance(val, bool):
                        raise _VolInvalid("Cannot coerce bool to int")
                    return target_type(val)
                except (ValueError, TypeError) as exc:
                    raise _VolInvalid(str(exc)) from exc

            return _coerce

        @staticmethod
        def Range(min: Optional[float] = None, max: Optional[float] = None) -> Callable[[Any], Any]:
            def _range(val: Any) -> Any:
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise _VolInvalid(f"Value {val!r} is not numeric")
                if min is not None and val < min:
                    raise _VolInvalid(f"Value {val} < min {min}")
                if max is not None and val > max:
                    raise _VolInvalid(f"Value {val} > max {max}")
                return val

            return _range

        @staticmethod
        def Clamp(min: Optional[float] = None, max: Optional[float] = None) -> Callable[[Any], Any]:
            def _clamp(val: Any) -> Any:
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise _VolInvalid(f"Value {val!r} is not numeric")
                res = val
                if min is not None and res < min:
                    res = type(val)(min)
                if max is not None and res > max:
                    res = type(val)(max)
                return res

            return _clamp

    vol = _VolModule()  # type: ignore[assignment]


try:
    from homeassistant.components.binary_sensor import (  # type: ignore[import-not-found]
        BinarySensorDeviceClass,
        BinarySensorEntity,
        BinarySensorEntityDescription,
    )
    from homeassistant.components.button import (  # type: ignore[import-not-found]
        ButtonDeviceClass,
        ButtonEntity,
        ButtonEntityDescription,
    )
    from homeassistant.components.camera import (  # type: ignore[import-not-found]
        Camera,
        CameraEntityFeature,
    )
    from homeassistant.components.event import (  # type: ignore[import-not-found]
        EventDeviceClass,
        EventEntity,
        EventEntityDescription,
    )
    from homeassistant.components.http import HomeAssistantView  # type: ignore[import-not-found]
    from homeassistant.components.image import (  # type: ignore[import-not-found]
        ImageEntity,
        ImageEntityDescription,
    )
    from homeassistant.components.media_player import MediaClass, MediaType  # type: ignore[import-not-found]
    from homeassistant.components.media_source import (  # type: ignore[import-not-found]
        BrowseMediaSource,
        MediaSource,
        MediaSourceItem,
        PlayMedia,
    )
    from homeassistant.components.number import (  # type: ignore[import-not-found]
        NumberDeviceClass,
        NumberEntity,
        NumberEntityDescription,
        NumberMode,
    )
    from homeassistant.components.select import (  # type: ignore[import-not-found]
        SelectEntity,
        SelectEntityDescription,
    )
    from homeassistant.components.sensor import (  # type: ignore[import-not-found]
        SensorDeviceClass,
        SensorEntity,
        SensorEntityDescription,
        SensorStateClass,
    )
    from homeassistant.components.switch import (  # type: ignore[import-not-found]
        SwitchDeviceClass,
        SwitchEntity,
        SwitchEntityDescription,
    )
    from homeassistant.config_entries import (  # type: ignore[import-not-found]
        ConfigEntry,
        ConfigFlow,
        OptionsFlow,
    )
    from homeassistant.const import EntityCategory, Platform  # type: ignore[import-not-found]
    from homeassistant.core import (  # type: ignore[import-not-found]
        HomeAssistant,
        ServiceCall,
        SupportsResponse,
        callback,
    )
    from homeassistant.exceptions import (  # type: ignore[import-not-found]
        ConfigEntryAuthFailed,
        ConfigEntryNotReady,
        HomeAssistantError,
        ServiceValidationError,
    )
    from homeassistant.helpers.device_registry import DeviceInfo  # type: ignore[import-not-found]
    from homeassistant.helpers.entity import Entity, EntityDescription  # type: ignore[import-not-found]
    from homeassistant.helpers.event import (  # type: ignore[import-not-found]
        async_call_later,
        async_track_point_in_time,
        async_track_time_change,
        async_track_time_interval,
    )
    from homeassistant.helpers.update_coordinator import (  # type: ignore[import-not-found]
        CoordinatorEntity,
        DataUpdateCoordinator,
        UpdateFailed,
    )
    from homeassistant.util import dt as dt_util  # type: ignore[import-not-found]

    HAS_HOMEASSISTANT = True

except ImportError:
    HAS_HOMEASSISTANT = False

    def callback(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "_hass_callback", True)
        return func

    class HomeAssistantError(Exception):
        """Base Home Assistant exception."""

    class ConfigEntryNotReady(HomeAssistantError):
        """Error when config entry is not ready."""

    class ConfigEntryAuthFailed(HomeAssistantError):
        """Error when config entry authentication failed."""

    class ServiceValidationError(HomeAssistantError):
        """Error when service call parameters fail validation."""

    class UpdateFailed(HomeAssistantError):
        """Raised when a DataUpdateCoordinator update fails."""

    class AbortFlow(HomeAssistantError):
        """Raised internally when a ConfigFlow aborts (e.g., already_configured)."""

        def __init__(self, reason: str, description_placeholders: Optional[Dict[str, Any]] = None) -> None:
            super().__init__(reason)
            self.reason = reason
            self.description_placeholders = description_placeholders

    class SupportsResponse(StrEnum):
        NONE = "none"
        OPTIONAL = "optional"
        ONLY = "only"

    class Platform(StrEnum):
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"
        NUMBER = "number"
        SWITCH = "switch"
        SELECT = "select"
        CAMERA = "camera"
        IMAGE = "image"
        EVENT = "event"

    class EntityCategory(StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class _DtUtil:
        UTC = timezone.utc

        @staticmethod
        def utcnow() -> datetime:
            return datetime.now(timezone.utc)

        @staticmethod
        def now(time_zone: Optional[timezone] = None) -> datetime:
            return datetime.now(time_zone) if time_zone else datetime.now().astimezone()

        @staticmethod
        def parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
            if not dt_str:
                return None
            try:
                dt = datetime.fromisoformat(str(dt_str))
                if dt.tzinfo is None:
                    dt = dt.astimezone()
                return dt
            except ValueError:
                return None

        @staticmethod
        def as_local(dattim: datetime) -> datetime:
            return dattim.astimezone()

        @staticmethod
        def as_utc(dattim: datetime) -> datetime:
            if dattim.tzinfo is None:
                dattim = dattim.astimezone()
            return dattim.astimezone(timezone.utc)

    dt_util = _DtUtil()

    @dataclasses.dataclass
    class ServiceCall:
        domain: str
        service: str
        data: Dict[str, Any] = dataclasses.field(default_factory=dict)
        context: Any = None
        return_response: bool = False

    class _HassConfig:
        def __init__(self, config_dir: Union[str, Path, None] = None) -> None:
            self.config_dir = str(Path(config_dir) if config_dir else Path("/tmp/ha_molly_cody_config"))
            self.time_zone = "UTC"

        def path(self, *parts: str) -> str:
            base = Path(self.config_dir)
            base.mkdir(parents=True, exist_ok=True)
            return str(base.joinpath(*parts))

    @dataclasses.dataclass
    class _BusEvent:
        event_type: str
        data: Dict[str, Any]
        time_fired: datetime = dataclasses.field(default_factory=dt_util.utcnow)

    class _EventBus:
        def __init__(self) -> None:
            self.events: List[Tuple[str, Dict[str, Any]]] = []
            self.fired_events: List[_BusEvent] = []
            self._listeners: Dict[str, List[Callable[..., Any]]] = {}

        @callback
        def async_fire(self, event_type: str, event_data: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
            payload = dict(event_data or {})
            self.events.append((event_type, payload))
            ev_obj = _BusEvent(event_type=event_type, data=payload)
            self.fired_events.append(ev_obj)
            for cb in list(self._listeners.get(event_type, [])) + list(self._listeners.get("*", [])):
                try:
                    res = cb(ev_obj)
                    if asyncio.iscoroutine(res):
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(res)
                        except RuntimeError:
                            pass
                except Exception:
                    pass

        @callback
        def async_listen(self, event_type: str, listener: Callable[..., Any]) -> Callable[[], None]:
            self._listeners.setdefault(event_type, []).append(listener)

            def _unsub() -> None:
                lst = self._listeners.get(event_type, [])
                if listener in lst:
                    lst.remove(listener)

            return _unsub

    class _ServiceRegistry:
        def __init__(self) -> None:
            self._services: Dict[Tuple[str, str], Dict[str, Any]] = {}

        @callback
        def async_register(
            self,
            domain: str,
            service: str,
            service_func: Callable[[ServiceCall], Any],
            schema: Any = None,
            supports_response: SupportsResponse = SupportsResponse.NONE,
        ) -> None:
            self._services[(domain, service)] = {
                "func": service_func,
                "schema": schema,
                "supports_response": supports_response,
            }

        @callback
        def async_remove(self, domain: str, service: str) -> None:
            self._services.pop((domain, service), None)

        def has_service(self, domain: str, service: str) -> bool:
            return (domain, service) in self._services

        def async_services(self) -> Dict[str, Dict[str, Any]]:
            out: Dict[str, Dict[str, Any]] = {}
            for (dom, svc), meta in self._services.items():
                out.setdefault(dom, {})[svc] = meta
            return out

        async def async_call(
            self,
            domain: str,
            service: str,
            service_data: Optional[Dict[str, Any]] = None,
            blocking: bool = True,
            return_response: bool = False,
            **kwargs: Any,
        ) -> Any:
            key = (domain, service)
            if key not in self._services:
                raise ServiceValidationError(f"Service {domain}.{service} not found")
            meta = self._services[key]
            data = dict(service_data or {})
            if meta["schema"] is not None and callable(meta["schema"]):
                data = meta["schema"](data)
            call = ServiceCall(
                domain=domain,
                service=service,
                data=data,
                return_response=return_response,
            )
            res = meta["func"](call)
            if asyncio.iscoroutine(res):
                return await res
            return res

    class _ConfigEntriesManager:
        def __init__(self, hass: Optional["HomeAssistant"] = None) -> None:
            self._hass = hass
            self._entries: Dict[str, "ConfigEntry"] = {}
            self.forwarded_platforms: Dict[str, List[str]] = {}

        def async_entries(self, domain: Optional[str] = None) -> List["ConfigEntry"]:
            if domain is None:
                return list(self._entries.values())
            return [e for e in self._entries.values() if e.domain == domain]

        def async_get_entry(self, entry_id: str) -> Optional["ConfigEntry"]:
            return self._entries.get(entry_id)

        @callback
        def async_update_entry(
            self,
            entry: "ConfigEntry",
            *,
            data: Optional[Mapping[str, Any]] = None,
            options: Optional[Mapping[str, Any]] = None,
            title: Optional[str] = None,
            unique_id: Optional[str] = None,
        ) -> bool:
            changed = False
            if data is not None and dict(data) != entry.data:
                entry.data = dict(data)
                changed = True
            if options is not None and dict(options) != entry.options:
                entry.options = dict(options)
                changed = True
            if title is not None and title != entry.title:
                entry.title = title
                changed = True
            if unique_id is not None and unique_id != entry.unique_id:
                entry.unique_id = unique_id
                changed = True
            self._entries[entry.entry_id] = entry
            for listener in list(entry.update_listeners):
                try:
                    res = listener(self._hass, entry)
                    if asyncio.iscoroutine(res):
                        try:
                            asyncio.get_running_loop().create_task(res)
                        except RuntimeError:
                            pass
                except Exception:
                    pass
            return changed

        async def async_forward_entry_setups(self, entry: "ConfigEntry", platforms: Iterable[str]) -> None:
            self._entries[entry.entry_id] = entry
            self.forwarded_platforms.setdefault(entry.entry_id, []).extend(str(p) for p in platforms)

        async def async_unload_platforms(self, entry: "ConfigEntry", platforms: Iterable[str]) -> bool:
            if entry.entry_id in self.forwarded_platforms:
                for p in platforms:
                    if str(p) in self.forwarded_platforms[entry.entry_id]:
                        self.forwarded_platforms[entry.entry_id].remove(str(p))
            return True

        async def async_reload(self, entry_id: str) -> bool:
            return True

    class _HassHttp:
        def __init__(self) -> None:
            self.views: List[Any] = []

        def register_view(self, view: Any) -> None:
            self.views.append(view)

    class HomeAssistant:
        """Standalone test & CLI compatible HomeAssistant instance."""

        def __init__(self, config_dir: Union[str, Path, None] = None) -> None:
            self.data: Dict[str, Any] = {}
            self.config = _HassConfig(config_dir)
            self.bus = _EventBus()
            self.services = _ServiceRegistry()
            self.config_entries = _ConfigEntriesManager(self)
            self.http = _HassHttp()
            self.is_running: bool = True
            self._tasks: List[asyncio.Task[Any]] = []

        async def async_add_executor_job(self, target: Callable[..., T], *args: Any) -> T:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, target, *args)

        def async_create_task(self, target: Awaitable[T], name: Optional[str] = None) -> asyncio.Task[T]:
            task = asyncio.ensure_future(target)
            self._tasks.append(task)
            return task

        async def async_block_till_done(self) -> None:
            pending = [t for t in self._tasks if not t.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    class ConfigEntry:
        """Standalone ConfigEntry representation."""

        def __init__(
            self,
            *,
            version: int = 1,
            minor_version: int = 1,
            domain: str = "molly_cody_feeder",
            title: str = "Molly & Cody Smart Feeder",
            data: Optional[Mapping[str, Any]] = None,
            options: Optional[Mapping[str, Any]] = None,
            source: str = "user",
            entry_id: str = "molly_cody_default_entry",
            unique_id: Optional[str] = None,
        ) -> None:
            self.version = version
            self.minor_version = minor_version
            self.domain = domain
            self.title = title
            self.data: Dict[str, Any] = dict(data or {})
            self.options: Dict[str, Any] = dict(options or {})
            self.source = source
            self.entry_id = entry_id
            self.unique_id = unique_id or self.data.get("device_id")
            self.update_listeners: List[Callable[..., Any]] = []
            self._on_unload: List[Callable[[], Any]] = []

        @callback
        def add_update_listener(self, listener: Callable[..., Any]) -> Callable[[], None]:
            self.update_listeners.append(listener)

            def _remove() -> None:
                if listener in self.update_listeners:
                    self.update_listeners.remove(listener)

            return _remove

        @callback
        def async_on_unload(self, func: Callable[[], Any]) -> None:
            self._on_unload.append(func)

        async def async_unload_callbacks(self) -> None:
            while self._on_unload:
                cb = self._on_unload.pop()
                res = cb()
                if asyncio.iscoroutine(res):
                    await res

    class DeviceInfo(dict):
        """Dictionary-backed DeviceInfo supporting both dict and attribute access."""

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

        def __getattr__(self, item: str) -> Any:
            try:
                return self[item]
            except KeyError as exc:
                raise AttributeError(item) from exc

    @dataclasses.dataclass(frozen=True)
    class EntityDescription:
        key: str
        name: Optional[str] = None
        icon: Optional[str] = None
        device_class: Optional[Any] = None
        entity_category: Optional[EntityCategory] = None
        translation_key: Optional[str] = None
        has_entity_name: bool = True
        entity_registry_enabled_default: bool = True

    class Entity:
        """Base Home Assistant Entity."""

        _attr_has_entity_name: bool = True
        _attr_name: Optional[str] = None
        _attr_unique_id: Optional[str] = None
        _attr_icon: Optional[str] = None
        _attr_device_info: Optional[DeviceInfo] = None
        _attr_entity_category: Optional[EntityCategory] = None
        _attr_extra_state_attributes: Optional[Dict[str, Any]] = None
        _attr_available: bool = True
        _attr_should_poll: bool = False
        _attr_translation_key: Optional[str] = None
        entity_description: Optional[EntityDescription] = None
        hass: Optional[HomeAssistant] = None
        entity_id: Optional[str] = None

        @property
        def name(self) -> Optional[str]:
            if self._attr_name is not None:
                return self._attr_name
            if self.entity_description is not None and self.entity_description.name is not None:
                return self.entity_description.name
            return None

        @property
        def unique_id(self) -> Optional[str]:
            return self._attr_unique_id

        @property
        def icon(self) -> Optional[str]:
            if self._attr_icon is not None:
                return self._attr_icon
            if self.entity_description is not None:
                return self.entity_description.icon
            return None

        @property
        def device_info(self) -> Optional[DeviceInfo]:
            return self._attr_device_info

        @property
        def entity_category(self) -> Optional[EntityCategory]:
            if self._attr_entity_category is not None:
                return self._attr_entity_category
            if self.entity_description is not None:
                return self.entity_description.entity_category
            return None

        @property
        def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
            return self._attr_extra_state_attributes

        @property
        def available(self) -> bool:
            return self._attr_available

        @callback
        def async_write_ha_state(self) -> None:
            pass

        async def async_added_to_hass(self) -> None:
            pass

        async def async_will_remove_from_hass(self) -> None:
            pass

        @callback
        def async_on_remove(self, func: Callable[[], Any]) -> None:
            pass

    class DataUpdateCoordinator(Generic[T]):
        """Standalone DataUpdateCoordinator implementation."""

        def __init__(
            self,
            hass: HomeAssistant,
            logger: logging.Logger,
            *,
            name: str = "molly_cody_feeder",
            update_interval: Optional[timedelta] = None,
            update_method: Optional[Callable[[], Awaitable[T]]] = None,
            config_entry: Optional[ConfigEntry] = None,
            **kwargs: Any,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.update_method = update_method
            self.config_entry = config_entry
            self.data: T = None  # type: ignore[assignment]
            self.last_update_success: bool = True
            self.last_exception: Optional[BaseException] = None
            self._listeners: List[Callable[[], None]] = []

        async def _async_update_data(self) -> T:
            if self.update_method is not None:
                return await self.update_method()
            raise NotImplementedError

        async def async_config_entry_first_refresh(self) -> None:
            try:
                self.data = await self._async_update_data()
                self.last_update_success = True
                self.last_exception = None
                self.async_update_listeners()
            except Exception as exc:
                self.last_update_success = False
                self.last_exception = exc
                if isinstance(exc, ConfigEntryNotReady):
                    raise
                raise ConfigEntryNotReady(str(exc)) from exc

        async def async_refresh(self) -> None:
            try:
                self.data = await self._async_update_data()
                self.last_update_success = True
                self.last_exception = None
            except Exception as exc:
                self.last_update_success = False
                self.last_exception = exc
                self.logger.warning("Error fetching %s data: %s", self.name, exc)
            self.async_update_listeners()

        async def async_request_refresh(self) -> None:
            await self.async_refresh()

        @callback
        def async_set_updated_data(self, data: T) -> None:
            self.data = data
            self.last_update_success = True
            self.last_exception = None
            self.async_update_listeners()

        @callback
        def async_add_listener(self, update_callback: Callable[[], None], context: Any = None) -> Callable[[], None]:
            self._listeners.append(update_callback)

            def _remove() -> None:
                if update_callback in self._listeners:
                    self._listeners.remove(update_callback)

            return _remove

        @callback
        def async_update_listeners(self) -> None:
            for cb in list(self._listeners):
                try:
                    cb()
                except Exception:
                    pass

    class CoordinatorEntity(Entity, Generic[T]):
        """Entity bound to a DataUpdateCoordinator."""

        def __init__(self, coordinator: DataUpdateCoordinator[T], context: Any = None) -> None:
            super().__init__()
            self.coordinator = coordinator
            self.coordinator_context = context
            self.hass = getattr(coordinator, "hass", None)

        @property
        def available(self) -> bool:
            return bool(self._attr_available and getattr(self.coordinator, "last_update_success", True))

        async def async_added_to_hass(self) -> None:
            await super().async_added_to_hass()
            self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator_update))

        @callback
        def _handle_coordinator_update(self) -> None:
            self.async_write_ha_state()

        async def async_update(self) -> None:
            await self.coordinator.async_request_refresh()

    # --- Sensor ---
    class SensorDeviceClass(StrEnum):
        TIMESTAMP = "timestamp"
        DURATION = "duration"
        WEIGHT = "weight"
        ENUM = "enum"
        SOUND_PRESSURE = "sound_pressure"
        DATA_RATE = "data_rate"

    class SensorStateClass(StrEnum):
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    @dataclasses.dataclass(frozen=True)
    class SensorEntityDescription(EntityDescription):
        state_class: Optional[SensorStateClass] = None
        native_unit_of_measurement: Optional[str] = None
        suggested_display_precision: Optional[int] = None
        options: Optional[List[str]] = None

    class SensorEntity(Entity):
        entity_description: SensorEntityDescription
        _attr_native_value: Any = None
        _attr_native_unit_of_measurement: Optional[str] = None
        _attr_device_class: Optional[SensorDeviceClass] = None
        _attr_state_class: Optional[SensorStateClass] = None

        @property
        def native_value(self) -> Any:
            return self._attr_native_value

        @property
        def state(self) -> Any:
            val = self.native_value
            if isinstance(val, datetime):
                return val.isoformat()
            return val

        @property
        def native_unit_of_measurement(self) -> Optional[str]:
            if self._attr_native_unit_of_measurement is not None:
                return self._attr_native_unit_of_measurement
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return getattr(self.entity_description, "native_unit_of_measurement", None)
            return None

        @property
        def device_class(self) -> Optional[Any]:
            if self._attr_device_class is not None:
                return self._attr_device_class
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.device_class
            return None

        @property
        def state_class(self) -> Optional[Any]:
            if self._attr_state_class is not None:
                return self._attr_state_class
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return getattr(self.entity_description, "state_class", None)
            return None

    # --- BinarySensor ---
    class BinarySensorDeviceClass(StrEnum):
        PROBLEM = "problem"
        CONNECTIVITY = "connectivity"
        RUNNING = "running"
        OCCUPANCY = "occupancy"
        MOTION = "motion"
        SOUND = "sound"
        SAFETY = "safety"
        TAMPER = "tamper"

    @dataclasses.dataclass(frozen=True)
    class BinarySensorEntityDescription(EntityDescription):
        pass

    class BinarySensorEntity(Entity):
        entity_description: BinarySensorEntityDescription
        _attr_is_on: Optional[bool] = None
        _attr_device_class: Optional[BinarySensorDeviceClass] = None

        @property
        def is_on(self) -> Optional[bool]:
            return self._attr_is_on

        @property
        def state(self) -> Optional[str]:
            if self.is_on is None:
                return None
            return "on" if self.is_on else "off"

        @property
        def device_class(self) -> Optional[Any]:
            if self._attr_device_class is not None:
                return self._attr_device_class
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.device_class
            return None

    # --- Button ---
    class ButtonDeviceClass(StrEnum):
        RESTART = "restart"
        UPDATE = "update"
        IDENTIFY = "identify"

    @dataclasses.dataclass(frozen=True)
    class ButtonEntityDescription(EntityDescription):
        pass

    class ButtonEntity(Entity):
        entity_description: ButtonEntityDescription

        async def async_press(self) -> None:
            raise NotImplementedError

    # --- Number ---
    class NumberDeviceClass(StrEnum):
        WEIGHT = "weight"
        DURATION = "duration"
        VOLUME = "volume"

    class NumberMode(StrEnum):
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    @dataclasses.dataclass(frozen=True)
    class NumberEntityDescription(EntityDescription):
        native_min_value: float = 0.0
        native_max_value: float = 100.0
        native_step: float = 1.0
        native_unit_of_measurement: Optional[str] = None
        mode: NumberMode = NumberMode.AUTO

    class NumberEntity(Entity):
        entity_description: NumberEntityDescription
        _attr_native_value: Optional[float] = None
        _attr_native_min_value: float = 0.0
        _attr_native_max_value: float = 100.0
        _attr_native_step: float = 1.0
        _attr_native_unit_of_measurement: Optional[str] = None
        _attr_mode: NumberMode = NumberMode.AUTO

        @property
        def native_value(self) -> Optional[float]:
            return self._attr_native_value

        @property
        def state(self) -> Optional[float]:
            return self.native_value

        @property
        def native_min_value(self) -> float:
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.native_min_value
            return self._attr_native_min_value

        @property
        def native_max_value(self) -> float:
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.native_max_value
            return self._attr_native_max_value

        @property
        def native_step(self) -> float:
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.native_step
            return self._attr_native_step

        @property
        def native_unit_of_measurement(self) -> Optional[str]:
            if self._attr_native_unit_of_measurement is not None:
                return self._attr_native_unit_of_measurement
            if hasattr(self, "entity_description") and self.entity_description is not None:
                return self.entity_description.native_unit_of_measurement
            return None

        async def async_set_native_value(self, value: float) -> None:
            raise NotImplementedError

    # --- Switch ---
    class SwitchDeviceClass(StrEnum):
        SWITCH = "switch"
        OUTLET = "outlet"

    @dataclasses.dataclass(frozen=True)
    class SwitchEntityDescription(EntityDescription):
        pass

    class SwitchEntity(Entity):
        entity_description: SwitchEntityDescription
        _attr_is_on: Optional[bool] = None

        @property
        def is_on(self) -> Optional[bool]:
            return self._attr_is_on

        @property
        def state(self) -> Optional[str]:
            if self.is_on is None:
                return None
            return "on" if self.is_on else "off"

        async def async_turn_on(self, **kwargs: Any) -> None:
            raise NotImplementedError

        async def async_turn_off(self, **kwargs: Any) -> None:
            raise NotImplementedError

    # --- Select ---
    @dataclasses.dataclass(frozen=True)
    class SelectEntityDescription(EntityDescription):
        options: Optional[List[str]] = None

    class SelectEntity(Entity):
        entity_description: SelectEntityDescription
        _attr_options: List[str] = []
        _attr_current_option: Optional[str] = None

        @property
        def options(self) -> List[str]:
            if self._attr_options:
                return self._attr_options
            if hasattr(self, "entity_description") and self.entity_description is not None and self.entity_description.options:
                return list(self.entity_description.options)
            return []

        @property
        def current_option(self) -> Optional[str]:
            return self._attr_current_option

        @property
        def state(self) -> Optional[str]:
            return self.current_option

        async def async_select_option(self, option: str) -> None:
            raise NotImplementedError

    # --- Camera ---
    class CameraEntityFeature(int):
        ON_OFF = 1
        STREAM = 2

    @dataclasses.dataclass(frozen=True)
    class CameraEntityDescription(EntityDescription):
        pass

    class Camera(Entity):
        entity_description: CameraEntityDescription
        _attr_is_recording: bool = False
        _attr_is_streaming: bool = False
        _attr_motion_detection_enabled: bool = False
        _attr_supported_features: int = 0
        content_type: str = "image/jpeg"

        def __init__(self) -> None:
            super().__init__()

        async def async_camera_image(
            self, width: Optional[int] = None, height: Optional[int] = None
        ) -> Optional[bytes]:
            return None

    # --- Image ---
    @dataclasses.dataclass(frozen=True)
    class ImageEntityDescription(EntityDescription):
        pass

    class ImageEntity(Entity):
        entity_description: ImageEntityDescription
        _attr_content_type: str = "image/jpeg"
        _attr_image_last_updated: Optional[datetime] = None

        def __init__(self, hass: Optional[HomeAssistant] = None, verify_ssl: bool = False) -> None:
            super().__init__()
            if hass is not None:
                self.hass = hass

        @property
        def content_type(self) -> str:
            return self._attr_content_type

        @property
        def image_last_updated(self) -> Optional[datetime]:
            return self._attr_image_last_updated

        @property
        def state(self) -> Optional[str]:
            return self.image_last_updated.isoformat() if self.image_last_updated else None

        def image(self) -> Optional[bytes]:
            return None

        async def async_image(self) -> Optional[bytes]:
            return self.image()

    # --- Event ---
    class EventDeviceClass(StrEnum):
        BUTTON = "button"
        DOORBELL = "doorbell"
        MOTION = "motion"

    @dataclasses.dataclass(frozen=True)
    class EventEntityDescription(EntityDescription):
        event_types: Optional[List[str]] = None

    class EventEntity(Entity):
        entity_description: EventEntityDescription
        _attr_event_types: List[str] = []
        _last_event_type: Optional[str] = None
        _last_event_attributes: Dict[str, Any] = {}
        _last_event_time: Optional[str] = None

        @property
        def event_types(self) -> List[str]:
            if self._attr_event_types:
                return list(self._attr_event_types)
            if hasattr(self, "entity_description") and self.entity_description is not None and self.entity_description.event_types:
                return list(self.entity_description.event_types)
            return []

        @property
        def state(self) -> Optional[str]:
            return self._last_event_time

        @property
        def extra_state_attributes(self) -> Dict[str, Any]:
            attrs = dict(self._attr_extra_state_attributes or {})
            if self._last_event_type is not None:
                attrs["event_type"] = self._last_event_type
            attrs.update(self._last_event_attributes)
            return attrs

        @callback
        def _trigger_event(self, event_type: str, event_attributes: Optional[Dict[str, Any]] = None) -> None:
            self._last_event_type = event_type
            self._last_event_attributes = dict(event_attributes or {})
            self._last_event_time = dt_util.utcnow().isoformat()
            self.async_write_ha_state()

    # --- ConfigFlow & OptionsFlow ---
    class ConfigFlow:
        VERSION: int = 1
        domain: str = "molly_cody_feeder"

        def __init_subclass__(cls, domain: Optional[str] = None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            if domain is not None:
                cls.domain = domain

        def __init__(self) -> None:
            self.hass: Optional[HomeAssistant] = None
            self.context: Dict[str, Any] = {"source": "user"}
            self._unique_id: Optional[str] = None
            self._existing_entries: List[ConfigEntry] = []

        @property
        def unique_id(self) -> Optional[str]:
            return self._unique_id

        def _async_current_entries(self, include_ignore: bool = True) -> List[ConfigEntry]:
            entries = list(self._existing_entries)
            if self.hass is not None and hasattr(self.hass, "config_entries"):
                for e in self.hass.config_entries.async_entries(self.domain):
                    if e not in entries:
                        entries.append(e)
            return entries

        async def async_set_unique_id(
            self, unique_id: Optional[str] = None, *, raise_on_progress: bool = True
        ) -> Optional[ConfigEntry]:
            self._unique_id = unique_id
            for entry in self._async_current_entries():
                if entry.unique_id == unique_id or entry.data.get("device_id") == unique_id:
                    return entry
            return None

        def _abort_if_unique_id_configured(
            self,
            updates: Optional[Dict[str, Any]] = None,
            reload_on_update: bool = True,
        ) -> None:
            if not self._unique_id:
                return
            for entry in self._async_current_entries():
                if entry.unique_id == self._unique_id or entry.data.get("device_id") == self._unique_id:
                    if updates and self.hass is not None:
                        self.hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
                    raise AbortFlow("already_configured")

        @callback
        def async_show_form(
            self,
            *,
            step_id: str,
            data_schema: Any = None,
            errors: Optional[Dict[str, str]] = None,
            description_placeholders: Optional[Dict[str, Any]] = None,
            last_step: Optional[bool] = None,
        ) -> Dict[str, Any]:
            return {
                "type": "form",
                "flow_id": "test_flow",
                "handler": self.domain,
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders,
                "last_step": last_step,
            }

        @callback
        def async_create_entry(
            self,
            *,
            title: str,
            data: Mapping[str, Any],
            description: Optional[str] = None,
            description_placeholders: Optional[Dict[str, Any]] = None,
            options: Optional[Mapping[str, Any]] = None,
        ) -> Dict[str, Any]:
            return {
                "type": "create_entry",
                "flow_id": "test_flow",
                "handler": self.domain,
                "version": self.VERSION,
                "title": title,
                "data": dict(data),
                "options": dict(options or {}),
                "description": description,
                "description_placeholders": description_placeholders,
                "result": ConfigEntry(
                    version=self.VERSION,
                    domain=self.domain,
                    title=title,
                    data=data,
                    options=options,
                    unique_id=self._unique_id or data.get("device_id"),
                ),
            }

        @callback
        def async_abort(
            self, *, reason: str, description_placeholders: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
            return {
                "type": "abort",
                "flow_id": "test_flow",
                "handler": self.domain,
                "reason": reason,
                "description_placeholders": description_placeholders,
            }

    class OptionsFlow:
        def __init__(self, config_entry: Optional[ConfigEntry] = None) -> None:
            self.hass: Optional[HomeAssistant] = None
            self._config_entry: Optional[ConfigEntry] = config_entry

        @property
        def config_entry(self) -> Optional[ConfigEntry]:
            return self._config_entry

        @config_entry.setter
        def config_entry(self, value: ConfigEntry) -> None:
            self._config_entry = value

        @callback
        def async_show_form(
            self,
            *,
            step_id: str,
            data_schema: Any = None,
            errors: Optional[Dict[str, str]] = None,
            description_placeholders: Optional[Dict[str, Any]] = None,
            last_step: Optional[bool] = None,
        ) -> Dict[str, Any]:
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders,
                "last_step": last_step,
            }

        @callback
        def async_create_entry(
            self,
            *,
            title: str = "",
            data: Mapping[str, Any],
            description: Optional[str] = None,
            description_placeholders: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            if self._config_entry is not None and self.hass is not None:
                self.hass.config_entries.async_update_entry(self._config_entry, options=dict(data))
            elif self._config_entry is not None:
                self._config_entry.options = dict(data)
            return {
                "type": "create_entry",
                "title": title,
                "data": dict(data),
                "description": description,
                "description_placeholders": description_placeholders,
            }

        @callback
        def async_abort(
            self, *, reason: str, description_placeholders: Optional[Dict[str, Any]] = None
        ) -> Dict[str, Any]:
            return {
                "type": "abort",
                "reason": reason,
                "description_placeholders": description_placeholders,
            }

    # --- Media Source & HTTP Views ---
    class MediaClass(StrEnum):
        DIRECTORY = "directory"
        VIDEO = "video"
        AUDIO = "audio"
        IMAGE = "image"

    class MediaType(StrEnum):
        VIDEO = "video"
        AUDIO = "audio"
        IMAGE = "image"
        CHANNEL = "channel"

    @dataclasses.dataclass
    class PlayMedia:
        url: str
        mime_type: str

    @dataclasses.dataclass
    class MediaSourceItem:
        hass: Optional[HomeAssistant]
        domain: str
        identifier: str
        target_media_player: Optional[str] = None

        @classmethod
        def from_uri(cls, hass: Optional[HomeAssistant], uri: str, target_media_player: Optional[str] = None) -> "MediaSourceItem":
            raw = uri[len("media-source://") :] if uri.startswith("media-source://") else uri
            parts = raw.split("/", 1)
            domain = parts[0]
            identifier = parts[1] if len(parts) > 1 else ""
            return cls(hass=hass, domain=domain, identifier=identifier, target_media_player=target_media_player)

    class BrowseMediaSource:
        def __init__(
            self,
            *,
            domain: Optional[str] = None,
            identifier: Optional[str] = None,
            media_class: str = MediaClass.DIRECTORY,
            media_content_type: str = "",
            title: str = "",
            can_play: bool = False,
            can_expand: bool = False,
            thumbnail: Optional[str] = None,
            children: Optional[List["BrowseMediaSource"]] = None,
            children_media_class: Optional[str] = None,
            not_shown: int = 0,
        ) -> None:
            self.domain = domain
            self.identifier = identifier
            self.media_class = media_class
            self.media_content_type = media_content_type
            self.title = title
            self.can_play = can_play
            self.can_expand = can_expand
            self.thumbnail = thumbnail
            self.children = children
            self.children_media_class = children_media_class
            self.not_shown = not_shown
            self.media_content_id = f"media-source://{domain}/{identifier}" if domain and identifier else f"media-source://{domain or ''}"

    class MediaSource:
        name: Optional[str] = None

        def __init__(self, domain: str) -> None:
            self.domain = domain

        async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
            raise NotImplementedError

        async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
            raise NotImplementedError

    class HomeAssistantView:
        url: str = ""
        name: str = ""
        requires_auth: bool = True
        extra_urls: List[str] = []

    # --- Event Tracking Helpers ---
    def async_track_time_interval(
        hass: HomeAssistant,
        action: Callable[[datetime], Any],
        interval: timedelta,
        *,
        cancel_on_shutdown: Optional[bool] = None,
    ) -> Callable[[], None]:
        cancelled = False
        task: Optional[asyncio.Task[Any]] = None

        async def _loop() -> None:
            sec = max(0.05, interval.total_seconds())
            while not cancelled:
                await asyncio.sleep(sec)
                if cancelled:
                    break
                res = action(dt_util.utcnow())
                if asyncio.iscoroutine(res):
                    await res

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_loop())
        except RuntimeError:
            pass

        def _unsub() -> None:
            nonlocal cancelled
            cancelled = True
            if task is not None and not task.done():
                task.cancel()

        return _unsub

    def async_track_point_in_time(
        hass: HomeAssistant,
        action: Callable[[datetime], Any],
        point_in_time: datetime,
    ) -> Callable[[], None]:
        delay = max(0.0, (point_in_time - dt_util.now(point_in_time.tzinfo)).total_seconds())
        return async_call_later(hass, delay, action)

    def async_track_time_change(
        hass: HomeAssistant,
        action: Callable[[datetime], Any],
        hour: Optional[Any] = None,
        minute: Optional[Any] = None,
        second: Optional[Any] = None,
    ) -> Callable[[], None]:
        cancelled = False

        def _unsub() -> None:
            nonlocal cancelled
            cancelled = True

        return _unsub

    def async_call_later(
        hass: HomeAssistant,
        delay: float,
        action: Callable[[datetime], Any],
    ) -> Callable[[], None]:
        cancelled = False
        task: Optional[asyncio.Task[Any]] = None

        async def _delayed() -> None:
            await asyncio.sleep(max(0.0, float(delay)))
            if not cancelled:
                res = action(dt_util.utcnow())
                if asyncio.iscoroutine(res):
                    await res

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_delayed())
        except RuntimeError:
            pass

        def _unsub() -> None:
            nonlocal cancelled
            cancelled = True
            if task is not None and not task.done():
                task.cancel()

        return _unsub


FlowResult = Dict[str, Any]


class _ConfigValidationShim:
    """Lightweight `homeassistant.helpers.config_validation` (`cv`) shim."""

    @staticmethod
    def string(value: Any) -> str:
        if value is None:
            raise vol.Invalid("string value cannot be None")
        return str(value)

    @staticmethod
    def boolean(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            val = value.strip().lower()
            if val in ("1", "true", "yes", "on", "enable"):
                return True
            if val in ("0", "false", "no", "off", "disable"):
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise vol.Invalid(f"Invalid boolean value {value!r}")

    @staticmethod
    def positive_int(value: Any) -> int:
        v = vol.Coerce(int)(value)
        if v < 0:
            raise vol.Invalid("Value must be at least 0")
        return v

    @staticmethod
    def port(value: Any) -> int:
        v = vol.Coerce(int)(value)
        if not (1 <= v <= 65535):
            raise vol.Invalid("Port must be between 1 and 65535")
        return v

    @staticmethod
    def config_entry_only_config_schema(domain: str) -> Callable[[Any], Any]:
        return lambda cfg: cfg


cv = _ConfigValidationShim()

