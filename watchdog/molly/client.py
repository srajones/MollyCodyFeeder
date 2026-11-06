"""MollyFeederClient: the public facade combining cloud session, device control, camera and speaker."""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

from watchdog.paths import PROJECT_ROOT
from watchdog.env import load_env
from watchdog.molly.camera import CameraMixin
from watchdog.molly.cloud import CloudSessionMixin
from watchdog.molly.device import DeviceControlMixin
from watchdog.molly.media import MediaEncodingMixin
from watchdog.molly.speaker import SpeakerMixin
from watchdog.molly.webrtc import WebRtcMixin


class MollyFeederClient(
    CloudSessionMixin,
    DeviceControlMixin,
    CameraMixin,
    SpeakerMixin,
    WebRtcMixin,
    MediaEncodingMixin,
):
    """Self-healing client for the Molly & Cody Smart Feeder 6L Camera (`and.mollycody.app01`).

    Composed of focused mixins (each in its own module):
    - CloudSessionMixin   (cloud.py)   account session + signed API
    - DeviceControlMixin  (device.py)  DPs, status, dispensing, schedule
    - CameraMixin         (camera.py)  snapshots + verification clips
    - SpeakerMixin        (speaker.py) TTS / audio playback
    - WebRtcMixin         (webrtc.py)  legacy fallback session
    - MediaEncodingMixin  (media.py)   MP4 / WAV writers
    """

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        country_code: str = "1",
        mode: Optional[str] = None,
        device_id: Optional[str] = None,
        local_ip: Optional[str] = None,
        local_key: Optional[str] = None,
        session_file: Optional[str] = None,
    ):
        load_env()
        self.email = email or os.getenv("MOLLY_USER") or os.getenv("USER", "")
        self.password = password or os.getenv("MOLLY_PASS") or os.getenv("PASS", "")
        self.country_code = country_code or os.getenv("COUNTRY_CODE", "1")
        raw_mode = (mode or os.getenv("FEEDER_MODE", "auto")).strip().lower()
        if raw_mode == "hybrid":
            raw_mode = "auto"
        self.mode = raw_mode if raw_mode in ("auto", "cloud", "lan") else "auto"

        self.device_id = device_id or os.getenv("TUYA_DEVICE_ID", "")
        self.local_ip = local_ip or os.getenv("FEEDER_LOCAL_IP", "")
        self.local_key = local_key or os.getenv("FEEDER_LOCAL_KEY", "")
        self.protocol_version = float(os.getenv("FEEDER_PROTOCOL_VERSION", "3.4"))

        default_sess_path = PROJECT_ROOT / "tuya_session.json"
        self.session_file = Path(session_file) if session_file else default_sess_path

        self.sid: str = ""
        self.uid: str = ""
        self.ecode: str = ""
        self.partner_id: str = "p2155085"
        self.gid: int = 0
        self.device_name: str = "M&C Smart Feeder 6L Camera"
        self.is_online: bool = True
        self.cached_dps: Dict[str, Any] = {}
        self._seq: int = int(time.time()) % 100000
        self._lock = threading.RLock()
        self._active_mqtt_client: Optional[mqtt.Client] = None
        self._dp_ack_evt = threading.Event()

        self._load_session()
