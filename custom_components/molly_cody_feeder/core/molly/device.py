"""DP read/write over Cloud HTTP+MQTT and Local LAN, plus feeding and schedule commands."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from custom_components.molly_cody_feeder.core.molly.constants import (
    DP_NAMES,
    MQTT_HOST,
    MQTT_PORT,
)
from custom_components.molly_cody_feeder.core.molly.crypto import (
    _decrypt_mqtt_22,
    _encrypt_mqtt_22,
)
from custom_components.molly_cody_feeder.core.molly.net import _never_sent
from custom_components.molly_cody_feeder.core.paths import DATA_DIR
from custom_components.molly_cody_feeder.core.schedule_decoder import (
    MealEntry,
    decode_molly_schedule,
    parse_days_spec,
    update_molly_schedule_slot,
)
import requests

try:
    import tinytuya  # type: ignore[import-not-found]
except ImportError:
    tinytuya = types.ModuleType("tinytuya")  # type: ignore[assignment]

    class _DummyOutletDevice:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def set_version(self, *args: Any, **kwargs: Any) -> None:
            pass

        def set_socketTimeout(self, *args: Any, **kwargs: Any) -> None:
            pass

        def status(self) -> Dict[str, Any]:
            return {"Error": "tinytuya not installed"}

        def set_multiple_values(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"Error": "tinytuya not installed"}

        def set_value(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            return {"Error": "tinytuya not installed"}

    tinytuya.OutletDevice = _DummyOutletDevice  # type: ignore[attr-defined]

logger = logging.getLogger("molly_cody_feeder.molly.device")


class DeviceDpsMixin:
    """Device DP access (cloud + LAN) and feeding/schedule helpers."""

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        country_code: str = "1",
        mode: Optional[str] = None,
        device_id: Optional[str] = None,
        local_ip: Optional[str] = None,
        local_key: Optional[str] = None,
        protocol_version: float = 3.4,
        session_file: Optional[str] = None,
    ):
        self.email = (email if email is not None else (os.getenv("MOLLY_USER") or os.getenv("USER", ""))).strip()
        self.password = (password if password is not None else (os.getenv("MOLLY_PASS") or os.getenv("PASS", ""))).strip()
        self.country_code = str(country_code or os.getenv("COUNTRY_CODE", "1")).strip()
        raw_mode = (mode or os.getenv("FEEDER_MODE", "cloud")).strip().lower()
        self.mode = raw_mode if raw_mode in ("cloud", "lan", "auto") else "cloud"

        self.device_id = (device_id or os.getenv("TUYA_DEVICE_ID", "")).strip()
        self.local_ip = (local_ip or os.getenv("FEEDER_LOCAL_IP", "")).strip()
        self.local_key = (local_key or os.getenv("FEEDER_LOCAL_KEY", "")).strip()
        self.protocol_version = float(protocol_version or os.getenv("FEEDER_PROTOCOL_VERSION", "3.4"))

        self.session_file = Path(
            session_file or os.getenv("TUYA_SESSION_FILE", str(DATA_DIR / "tuya_session.json"))
        )

        self.sid = ""
        self.uid = ""
        self.ecode = ""
        self.partner_id = "p2155085"
        self.gid = 0
        self.device_name = "M&C Smart Feeder 6L Camera"
        self.mobile_device_id = "89852002387643018592011482019482"
        self.cached_dps: Dict[str, Any] = {}
        self._cloud_online: bool = False
        self._lock = threading.RLock()
        self._active_mqtt_client: Optional[Any] = None
        self._dp_ack_evt = threading.Event()

        self._load_session()

    def set_mode(self, mode: str) -> str:
        m = mode.strip().lower()
        if m == "hybrid":
            m = "auto"
        if m not in ("cloud", "lan", "auto"):
            raise ValueError(f"Invalid mode '{mode}'. Choose 'cloud', 'lan', or 'auto'.")
        self.mode = m
        return self.mode

    def _get_lan_device(self, timeout: float = 3.0) -> Any:
        if not self.device_id or not self.local_ip or not self.local_key:
            return None
        dev = tinytuya.OutletDevice(
            dev_id=self.device_id,
            address=self.local_ip,
            local_key=self.local_key,
        )
        dev.set_version(self.protocol_version)
        dev.set_socketTimeout(timeout)
        return dev

    def get_dps_lan(self) -> Optional[Dict[str, Any]]:
        dev = self._get_lan_device(timeout=3.0)
        if not dev:
            return None
        try:
            status = dev.status()
            if isinstance(status, dict) and isinstance(status.get("dps"), dict):
                dps = {str(k): v for k, v in status["dps"].items()}
                self.cached_dps.update(dps)
                return dict(self.cached_dps)
        except Exception as exc:
            logger.debug("LAN status query failed: %s", exc)
        return None

    def set_dps_lan(self, dps: Dict[str, Any]) -> bool:
        dev = self._get_lan_device(timeout=4.0)
        if not dev:
            return False
        try:
            str_dps = {str(k): v for k, v in dps.items()}
            if len(str_dps) == 1:
                k, v = next(iter(str_dps.items()))
                res = dev.set_value(int(k), v)
            else:
                res = dev.set_multiple_values(str_dps)
            if isinstance(res, dict) and "Error" in res:
                logger.warning("LAN DP write error: %s", res)
                return False
            self.cached_dps.update(str_dps)
            return True
        except Exception as exc:
            logger.warning("LAN DP write exception: %s", exc)
            return False

    def get_dps_cloud(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.sid:
                self.login(force=True)
            if self.refresh_cloud_devices():
                return dict(self.cached_dps)
            if self.login(force=True):
                return dict(self.cached_dps)
        except Exception as exc:
            logger.error("Cloud DP status fetch failed: %s", exc)
        return dict(self.cached_dps) if self.cached_dps else None

    def set_dps_cloud(self, dps: Dict[str, Any], wait_ack_sec: float = 2.0) -> bool:
        str_dps = {str(k): v for k, v in dps.items()}
        for attempt in range(3):
            try:
                pub_res = self.call_api(
                    "thing.m.device.dp.publish",
                    "1.0",
                    {
                        "gwId": self.device_id,
                        "devId": self.device_id,
                        "dps": json.dumps(str_dps, separators=(",", ":")),
                    },
                    allow_relogin=True,
                )
                if isinstance(pub_res, dict) and pub_res.get("success") and pub_res.get("result") is not False:
                    self.cached_dps.update(str_dps)
                    self._save_session()
                    return True
                logger.warning("Cloud HTTP dp.publish returned non-success (try %d/3): %s", attempt + 1, pub_res)
            except requests.exceptions.RequestException as exc:
                if not _never_sent(exc):
                    logger.warning("Cloud HTTP dp.publish failed (not retried, outcome unknown): %s", exc)
                    return False
                logger.warning("Cloud HTTP dp.publish could not connect (try %d/3): %s", attempt + 1, exc)
            except Exception as exc:
                logger.warning("Cloud HTTP dp.publish failed (not retried, outcome unknown): %s", exc)
                return False
            time.sleep(2.0 * (attempt + 1))
        return False

    def get_status(self) -> Dict[str, Any]:
        dps: Optional[Dict[str, Any]] = None
        used_transport = "none"

        if self.mode in ("lan", "auto"):
            dps = self.get_dps_lan()
            if dps is not None:
                used_transport = "lan"

        if dps is None and self.mode in ("cloud", "auto"):
            dps = self.get_dps_cloud()
            if dps is not None:
                used_transport = "cloud"

        dps = dps or dict(self.cached_dps)
        named_dps = {DP_NAMES.get(str(k), f"dp_{k}"): v for k, v in dps.items()}
        sched_raw = str(dps.get("207", ""))
        meals = decode_molly_schedule(sched_raw)
        online = used_transport in ("lan", "cloud") and (used_transport == "lan" or self._cloud_online or bool(dps))

        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "configured_mode": self.mode,
            "active_transport": used_transport,
            "local_ip": self.local_ip,
            "online": online,
            "dps": dps,
            "named_dps": named_dps,
            "food_weight_g": dps.get("202"),
            "last_feed_portions": dps.get("201"),
            "feed_abnormal": dps.get("255", 0),
            "schedule_raw": sched_raw,
            "schedule": [m.to_dict() for m in meals],
        }

    def set_dps(self, dps: Dict[str, Any]) -> Tuple[bool, str]:
        if self.mode == "lan":
            ok = self.set_dps_lan(dps)
            return ok, "lan"
        if self.mode == "cloud":
            ok = self.set_dps_cloud(dps)
            return ok, "cloud"
        if self.set_dps_lan(dps):
            return True, "lan"
        ok = self.set_dps_cloud(dps)
        return ok, "cloud"

    def set_dp(self, dp_id: int | str, value: Any) -> Tuple[bool, str]:
        return self.set_dps({str(dp_id): value})

    def dispense_food(self, portions: int = 1) -> Tuple[bool, str]:
        portions = max(1, min(20, int(portions)))
        ok, transport = self.set_dp(201, portions)
        logger.info("Dispense %d portion(s) via %s -> ok=%s", portions, transport, ok)
        return ok, transport

    def get_schedule(self) -> List[MealEntry]:
        status = self.get_status()
        return decode_molly_schedule(str(status.get("schedule_raw", "")))

    def set_schedule_raw(self, raw_schedule: str) -> Tuple[bool, str]:
        if not isinstance(raw_schedule, str) or len(raw_schedule) < 15 or len(raw_schedule) % 15 != 0:
            raise ValueError("Invalid Molly & Cody schedule string length (must be a multiple of 15 chars)")
        return self.set_dp(207, raw_schedule)

    def update_schedule_slot(
        self,
        slot: int,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        portions: Optional[int] = None,
        days_mask: Optional[int] = None,
        enabled: Optional[bool] = None,
        days: Optional[Any] = None,
    ) -> Tuple[bool, str, List[MealEntry]]:
        if days_mask is None and days is not None:
            if isinstance(days, int):
                days_mask = days
            else:
                days_mask = parse_days_spec(str(days))
        status = self.get_status()
        current_raw = str(status.get("schedule_raw", ""))
        new_raw = update_molly_schedule_slot(
            current_raw=current_raw,
            slot=slot,
            hour=hour,
            minute=minute,
            portions=portions,
            days_mask=days_mask,
            enabled=enabled,
        )
        ok, transport = self.set_dp(207, new_raw)
        return ok, transport, decode_molly_schedule(new_raw)
