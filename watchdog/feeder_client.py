import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional
import requests

try:
    import tinytuya
except ImportError:
    tinytuya = None

from watchdog.config import WatchdogConfig

logger = logging.getLogger("poor_puppy.feeder_client")

TUYA_ENDPOINTS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}


class FeederClient:
    """
    Unified client for Molly & Cody (Thingclips / Tuya OEM) Smart Pet Feeders.
    Supports both Local LAN control (via tinytuya TCP 6668) and Tuya Cloud OpenAPI
    (for remote VPS control, DP status inspection, and IPC RTSP/HLS stream allocation).
    """

    def __init__(self, config: WatchdogConfig):
        self.cfg = config
        self.base_url = TUYA_ENDPOINTS.get(config.tuya_region.lower(), TUYA_ENDPOINTS["us"])
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # -------------------------------------------------------------------------
    # Local Tuya (TCP port 6668 via tinytuya)
    # -------------------------------------------------------------------------
    def _get_local_device(self):
        if tinytuya is None:
            raise RuntimeError("tinytuya is not installed. Run `pip install tinytuya`.")
        if not (self.cfg.tuya_device_id and self.cfg.feeder_local_ip and self.cfg.feeder_local_key):
            raise ValueError("Local Tuya requires TUYA_DEVICE_ID, FEEDER_LOCAL_IP, and FEEDER_LOCAL_KEY.")
        dev = tinytuya.Device(
            dev_id=self.cfg.tuya_device_id,
            address=self.cfg.feeder_local_ip,
            local_key=self.cfg.feeder_local_key,
            version=self.cfg.feeder_protocol_version,
        )
        dev.set_socketTimeout(5)
        return dev

    def get_local_status(self) -> Dict[str, Any]:
        """Reads current Data Points (DPs) directly from the feeder on the LAN."""
        dev = self._get_local_device()
        data = dev.status()
        logger.debug("Local Tuya status response: %s", data)
        return data.get("dps", {}) if isinstance(data, dict) else {}

    def trigger_local_feed(self, portions: int = 1) -> bool:
        """Dispenses `portions` immediately over Local LAN TCP 6668."""
        dev = self._get_local_device()
        res = dev.set_value(int(self.cfg.dp_manual_feed), portions)
        logger.info("Triggered local manual feed (%d portions): %s", portions, res)
        return isinstance(res, dict) and "Error" not in res

    # -------------------------------------------------------------------------
    # Tuya Cloud OpenAPI (HMAC-SHA256 v2.0 signing)
    # -------------------------------------------------------------------------
    def _sign_request(
        self, method: str, path: str, body: str = "", access_token: str = ""
    ) -> Dict[str, str]:
        t = str(int(time.time() * 1000))
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        string_to_sign = f"{method.upper()}\n{body_hash}\n\n{path}"
        sign_str = f"{self.cfg.tuya_client_id}{access_token}{t}{string_to_sign}"
        signature = (
            hmac.new(
                self.cfg.tuya_client_secret.encode("utf-8"),
                sign_str.encode("utf-8"),
                hashlib.sha256,
            )
            .hexdigest()
            .upper()
        )
        headers = {
            "client_id": self.cfg.tuya_client_id,
            "sign": signature,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if access_token:
            headers["access_token"] = access_token
        return headers

    def _get_cloud_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        if not (self.cfg.tuya_client_id and self.cfg.tuya_client_secret):
            raise ValueError("TUYA_CLIENT_ID and TUYA_CLIENT_SECRET are required for Cloud mode.")

        path = "/v1.0/token?grant_type=1"
        headers = self._sign_request("GET", path)
        resp = requests.get(f"{self.base_url}{path}", headers=headers, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"Tuya token request failed: {payload}")
        result = payload["result"]
        self._access_token = result["access_token"]
        self._token_expires_at = time.time() + int(result.get("expire_time", 7200)) - 60
        return self._access_token

    def cloud_request(self, method: str, path: str, body_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        token = self._get_cloud_token()
        body_str = json.dumps(body_dict) if body_dict is not None else ""
        headers = self._sign_request(method, path, body=body_str, access_token=token)
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=headers, data=body_str if body_str else None, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_cloud_status(self) -> Dict[str, Any]:
        """Fetches device status & DPs from Tuya Cloud."""
        path = f"/v1.0/devices/{self.cfg.tuya_device_id}"
        res = self.cloud_request("GET", path)
        if not res.get("success"):
            logger.error("Failed to fetch cloud device status: %s", res)
            return {}
        status_list = res.get("result", {}).get("status", [])
        return {item["code"]: item["value"] for item in status_list if "code" in item}

    def trigger_cloud_feed(self, portions: int = 1, code: str = "manual_feed") -> bool:
        """Triggers manual feed command via Tuya Cloud API."""
        path = f"/v1.0/iot-03/devices/{self.cfg.tuya_device_id}/commands"
        body = {"commands": [{"code": code, "value": portions}]}
        res = self.cloud_request("POST", path, body)
        logger.info("Triggered cloud manual feed (%d portions): %s", portions, res)
        return bool(res.get("success"))

    def allocate_ipc_stream(self, stream_type: str = "rtsp") -> Optional[str]:
        """
        Requests a live RTSP or HLS stream URL from Tuya Cloud IPC service
        (matches TUNIRTSPMediaManager / IPCKit v7.10 in the Molly & Cody APK).
        """
        if not self.cfg.tuya_uid:
            logger.warning("TUYA_UID not set; cannot allocate cloud IPC stream.")
            return None
        path = f"/v1.0/users/{self.cfg.tuya_uid}/devices/{self.cfg.tuya_device_id}/stream/actions/allocate"
        res = self.cloud_request("POST", path, {"type": stream_type})
        if res.get("success") and "result" in res:
            return res["result"].get("url")
        logger.error("Failed to allocate IPC %s stream: %s", stream_type, res)
        return None

    # -------------------------------------------------------------------------
    # High-Level Unified Operations
    # -------------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        if self.cfg.feeder_mode in ("local", "hybrid") and self.cfg.feeder_local_ip and self.cfg.feeder_local_key:
            try:
                dps = self.get_local_status()
                if dps:
                    return dps
            except Exception as exc:
                logger.warning("Local status check failed, falling back to cloud: %s", exc)
        if self.cfg.feeder_mode in ("cloud", "hybrid") and self.cfg.tuya_client_id:
            return self.get_cloud_status()
        return {}

    def dispense_food(self, portions: int = 1) -> bool:
        if self.cfg.feeder_mode in ("local", "hybrid") and self.cfg.feeder_local_ip and self.cfg.feeder_local_key:
            try:
                if self.trigger_local_feed(portions):
                    return True
            except Exception as exc:
                logger.warning("Local dispense failed, falling back to cloud: %s", exc)
        if self.cfg.feeder_mode in ("cloud", "hybrid") and self.cfg.tuya_client_id:
            return self.trigger_cloud_feed(portions)
        return False

    def get_stream_url(self) -> str:
        """Returns configured RTSP/HLS stream URL or dynamically allocates one from Tuya Cloud."""
        if self.cfg.camera_stream_url:
            return self.cfg.camera_stream_url
        url = self.allocate_ipc_stream("rtsp") or self.allocate_ipc_stream("hls")
        if not url:
            raise RuntimeError("No CAMERA_STREAM_URL configured and Cloud IPC stream allocation failed.")
        return url
