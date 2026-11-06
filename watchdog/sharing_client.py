import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tuya_sharing import CustomerDevice, Manager, SharingDeviceListener, SharingTokenListener

from watchdog.paths import PROJECT_ROOT
from watchdog.schedule_decoder import MealEntry, decode_meal_plan

logger = logging.getLogger("poor_puppy.sharing_client")

HA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
# Discovered from AndroidManifest.xml inside smartfeeder.apks (and.mollycody.app01):
MC_SCHEMA = "mollycodyapp01"
DEFAULT_SESSION_FILE = PROJECT_ROOT / "tuya_session.json"


class _TokenPersistenceListener(SharingTokenListener):
    def __init__(self, session_file: Path, base_data: Dict[str, Any]):
        self.session_file = session_file
        self.base_data = base_data

    def update_token(self, token_info: Dict[str, Any]):
        self.base_data["token_info"] = token_info
        self.session_file.write_text(json.dumps(self.base_data, indent=2))
        logger.info("Refreshed and saved Tuya session token to %s", self.session_file)


class _FeederEventListener(SharingDeviceListener):
    def __init__(self, on_report: Optional[Callable[[CustomerDevice, List[str]], None]] = None):
        self.on_report = on_report

    def update_device(
        self,
        device: CustomerDevice,
        updated_status_properties: Optional[List[str]] = None,
        dp_timestamps: Optional[Dict] = None,
    ) -> None:
        logger.info(
            "Real-time MQTT update from %s (%s): updated=%s status=%s",
            device.name,
            device.id,
            updated_status_properties,
            device.status,
        )
        if self.on_report and updated_status_properties:
            self.on_report(device, updated_status_properties)

    def add_device(self, device: CustomerDevice):
        pass

    def remove_device(self, device_id: str):
        pass


class TuyaSharingFeederClient:
    """
    Lightweight pure-Python client built on `tuya-device-sharing-sdk` using the user's
    USER_CODE (from .env), schema `mollycodyapp01`, and persisted `tuya_session.json`.
    """

    def __init__(self, user_code: str, session_file: Path = DEFAULT_SESSION_FILE):
        self.user_code = user_code
        self.session_file = session_file
        self.manager: Optional[Manager] = None
        self.feeder_device: Optional[CustomerDevice] = None

    def connect(self) -> CustomerDevice:
        if not self.session_file.exists():
            raise FileNotFoundError(
                f"Session file {self.session_file} not found. Run `python scripts/link_and_inspect.py` first."
            )
        data = json.loads(self.session_file.read_text())
        token_listener = _TokenPersistenceListener(self.session_file, data)
        self.manager = Manager(
            client_id=HA_CLIENT_ID,
            user_code=data.get("user_code", self.user_code),
            terminal_id=data["terminal_id"],
            end_point=data["endpoint"],
            token_response=data["token_info"],
            listener=token_listener,
        )
        self.manager.update_device_cache()
        if not self.manager.device_map:
            raise RuntimeError("No devices found on this account.")

        # Prefer online feeder / camera feeder
        candidates = list(self.manager.device_map.values())
        online_candidates = [d for d in candidates if d.online]
        search_pool = online_candidates if online_candidates else candidates

        for dev in search_pool:
            if dev.category in ("cwwsq", "sp") or "feed" in dev.name.lower() or "molly" in dev.name.lower():
                self.feeder_device = dev
                break
        if not self.feeder_device:
            self.feeder_device = search_pool[0]

        logger.info(
            "Connected to feeder '%s' (id=%s, category=%s, online=%s)",
            self.feeder_device.name,
            self.feeder_device.id,
            self.feeder_device.category,
            self.feeder_device.online,
        )
        return self.feeder_device

    def get_scheduled_meals(self) -> List[MealEntry]:
        """Extracts and decodes the feeding schedule (`meal_plan`) directly from the feeder's DPs."""
        if not self.feeder_device:
            self.connect()
        status = self.feeder_device.status or {}
        for key in ("meal_plan", "feed_plan", "1"):
            if key in status and status[key]:
                meals = decode_meal_plan(status[key])
                if meals:
                    return [m for m in meals if m.enabled]
        return []

    def allocate_stream_url(self) -> Optional[str]:
        """Allocates a live RTSP or HLS video+audio stream from the feeder's camera."""
        if not self.manager or not self.feeder_device:
            self.connect()
        for stream_type in ("rtsp", "hls", "flv"):
            try:
                url = self.manager.get_device_stream_allocate(self.feeder_device.id, stream_type)
                if url:
                    logger.info("Allocated %s stream URL for feeder camera", stream_type.upper())
                    return url
            except Exception as exc:
                logger.warning("Stream allocation (%s) failed: %s", stream_type, exc)
        return None

    def trigger_manual_feed(self, portions: int = 1) -> None:
        """Dispenses `portions` immediately."""
        if not self.manager or not self.feeder_device:
            self.connect()
        feed_code = "manual_feed"
        for candidate in ("manual_feed", "feed_num", "quick_feed"):
            if candidate in (self.feeder_device.function or {}):
                feed_code = candidate
                break
        self.manager.send_commands(self.feeder_device.id, [{"code": feed_code, "value": portions}])
        logger.info("Sent %s=%d command to feeder %s", feed_code, portions, self.feeder_device.id)

    def start_mqtt_listener(self, on_report: Callable[[CustomerDevice, List[str]], None]) -> None:
        if not self.manager or not self.feeder_device:
            self.connect()
        self.feeder_device.set_up = True
        self.manager.add_device_listener(_FeederEventListener(on_report=on_report))
        self.manager.refresh_mq()
        logger.info("Started real-time Tuya MQTT listener for feeder events.")
