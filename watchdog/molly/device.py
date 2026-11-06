"""Device control: data points (DPs) over cloud/LAN, status, dispensing and the meal schedule."""

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import tinytuya

from watchdog.schedule_decoder import MealEntry, decode_molly_schedule, update_molly_schedule_slot
from watchdog.molly.constants import DP_NAMES, MQTT_HOST
from watchdog.molly.net import _never_sent

logger = logging.getLogger("poor_puppy.molly.device")


class DeviceControlMixin:
    """Read/write feeder data points, dispense food, read/update the DP 207 schedule."""

    # -------------------------------------------------------------------------
    # Toggleable LAN vs. CLOUD Status & DP Control
    # -------------------------------------------------------------------------
    def set_mode(self, mode: str) -> str:
        m = mode.strip().lower()
        if m == "hybrid":
            m = "auto"
        if m not in ("auto", "cloud", "lan"):
            raise ValueError(f"Invalid mode '{mode}'; must be 'auto', 'cloud', or 'lan'")
        self.mode = m
        logger.info("Feeder connection mode set to: %s", self.mode.upper())
        return self.mode

    def get_dps_lan(self) -> Optional[Dict[str, Any]]:
        """Reads live DPs directly from the feeder over LAN TCP 6668 (protocol v3.4)."""
        if not self.local_ip or not self.local_key:
            return None
        try:
            dev = tinytuya.Device(
                self.device_id,
                self.local_ip,
                self.local_key,
                version=self.protocol_version,
            )
            dev.set_socketTimeout(2.5)
            dev.set_socketRetryLimit(1)
            res = dev.status()
            if isinstance(res, dict) and isinstance(res.get("dps"), dict):
                self.cached_dps.update({str(k): v for k, v in res["dps"].items()})
                return self.cached_dps
        except Exception as exc:
            logger.debug("LAN status check failed (%s): %s", self.local_ip, exc)
        return None

    def get_dps_cloud(self) -> Optional[Dict[str, Any]]:
        """Reads live DPs from the Molly & Cody Cloud API (works on remote VPS without LAN)."""
        try:
            if self.refresh_cloud_devices(allow_relogin=True):
                return self.cached_dps
        except Exception as exc:
            logger.error("Cloud status check failed: %s", exc)
        return None

    def get_status(self) -> Dict[str, Any]:
        """
        Returns full feeder status dictionary according to `self.mode` ('auto', 'cloud', 'lan'),
        including decoded DPs, decoded meal schedule, portion weight, and online state.
        """
        source = "none"
        dps: Optional[Dict[str, Any]] = None

        if self.mode in ("lan", "auto"):
            dps = self.get_dps_lan()
            if dps is not None:
                source = "lan"

        if dps is None and self.mode in ("cloud", "auto"):
            dps = self.get_dps_cloud()
            if dps is not None:
                source = "cloud"

        if dps is None:
            dps = self.cached_dps

        named_dps = {DP_NAMES.get(str(k), f"dp_{k}"): v for k, v in dps.items()}
        raw_sched = str(dps.get("207", ""))
        meals = decode_molly_schedule(raw_sched)

        return {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "configured_mode": self.mode,
            "active_transport": source,
            "local_ip": self.local_ip,
            "online": self.is_online if source != "none" else False,
            "dps": dps,
            "named_dps": named_dps,
            "food_weight_g": dps.get("202"),
            "last_feed_portions": dps.get("201"),
            "feed_abnormal": dps.get("255", 0),
            "schedule_raw": raw_sched,
            "schedule": [m.to_dict() for m in meals],
        }

    def set_dps_lan(self, dps: Dict[str, Any]) -> bool:
        """Sends DP commands directly over LAN TCP 6668."""
        if not self.local_ip or not self.local_key:
            return False
        try:
            dev = tinytuya.Device(
                self.device_id,
                self.local_ip,
                self.local_key,
                version=self.protocol_version,
            )
            dev.set_socketTimeout(3.0)
            dev.set_socketRetryLimit(1)
            res = dev.set_multiple_values(dps)
            if isinstance(res, dict) and "Error" not in res:
                self.cached_dps.update({str(k): v for k, v in dps.items()})
                return True
        except Exception as exc:
            logger.debug("LAN set_dps failed: %s", exc)
        return False

    def set_dps_cloud(self, dps: Dict[str, Any], wait_ack_sec: float = 3.5) -> bool:
        """
        Sends DP commands over the Molly & Cody Cloud HTTPS API (`thing.m.device.dp.publish` v1.0),
        which works from any remote VPS without LAN and without disturbing active MQTT streams.
        """
        str_dps = {str(k): v for k, v in dps.items()}
        for attempt in range(3):
            try:
                res = self.call_api(
                    "thing.m.device.dp.publish",
                    "1.0",
                    {
                        "gwId": self.device_id,
                        "devId": self.device_id,
                        "dps": json.dumps(str_dps, separators=(",", ":")),
                    },
                    allow_relogin=True,
                )
                if res.get("success") and res.get("result") is True:
                    self.cached_dps.update(str_dps)
                    self._save_session()
                    return True
                # The server answered "no": nothing was actuated, so a retry can't double-feed
                logger.warning("Cloud HTTP dp.publish returned non-success (try %d/3): %s", attempt + 1, res)
            except requests.exceptions.RequestException as exc:
                if not _never_sent(exc):
                    # e.g. read timeout / connection aborted: the command MAY have been delivered
                    logger.warning("Cloud HTTP dp.publish failed (not retried, outcome unknown): %s", exc)
                    return False
                # DNS / TCP connect failure: the request never left -> safe to retry
                logger.warning("Cloud HTTP dp.publish could not connect (try %d/3): %s", attempt + 1, exc)
            except Exception as exc:
                # e.g. a read timeout: the command MAY have been delivered -> don't risk a double feed
                logger.warning("Cloud HTTP dp.publish failed (not retried, outcome unknown): %s", exc)
                return False
            time.sleep(2.0 * (attempt + 1))
        return False

    def set_dps(self, dps: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Sets one or more DPs on the feeder.
        Note: On the Molly & Cody Smart Feeder 6L Camera (`yv8xga2javjxtqdc`), MCU and IPC
        control DPs (201 manual feed, 207 schedule, 108 nightvision, etc.) are actuated via
        MQTT `smart/mb/in/{devId}` (protocol 5, pv 2.2), so Cloud MQTT is used first in
        'auto' and 'cloud' modes, with LAN TCP 6668 as fallback.
        """
        if self.mode in ("cloud", "auto"):
            if self.set_dps_cloud(dps):
                logger.info("Set DPs %s via CLOUD MQTT (%s)", dps, MQTT_HOST)
                return True, "cloud"
            if self.mode == "cloud":
                return False, "cloud"

        if self.mode in ("lan", "auto"):
            if self.set_dps_lan(dps):
                logger.info("Set DPs %s via LAN (%s)", dps, self.local_ip)
                return True, "lan"

        return False, "none"

    def set_dp(self, dp_id: int | str, value: Any) -> Tuple[bool, str]:
        return self.set_dps({str(dp_id): value})

    # -------------------------------------------------------------------------
    # High-Level Feeder Actions (Dispense, Schedule Management, Camera Settings)
    # -------------------------------------------------------------------------
    def dispense_food(self, portions: int = 1) -> Tuple[bool, str]:
        """Triggers the feeder to dispense `portions` (1..20) immediately via DP 201."""
        portions = max(1, min(20, int(portions)))
        return self.set_dp(201, portions)

    def get_schedule(self) -> Tuple[str, List[MealEntry]]:
        """Fetches the current DP 207 schedule string and decoded MealEntry slots."""
        status = self.get_status()
        raw = str(status.get("schedule_raw", ""))
        return raw, decode_molly_schedule(raw)

    def set_schedule_raw(self, raw_schedule: str) -> Tuple[bool, str]:
        """Writes a raw 15-char-per-slot schedule string to DP 207."""
        if len(raw_schedule) < 15 or len(raw_schedule) % 15 != 0:
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
    ) -> Tuple[bool, str, List[MealEntry]]:
        """Updates a single schedule slot (1..8) in DP 207 and returns `(ok, transport, updated_meals)`."""
        raw, _ = self.get_schedule()
        new_raw = update_molly_schedule_slot(
            current_raw=raw,
            slot=slot,
            hour=hour,
            minute=minute,
            portions=portions,
            days_mask=days_mask,
            enabled=enabled,
        )
        ok, transport = self.set_schedule_raw(new_raw)
        return ok, transport, decode_molly_schedule(new_raw)
