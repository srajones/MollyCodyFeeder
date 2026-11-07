"""RuntimeConfig & WatchdogConfig for the Molly & Cody Smart Feeder HACS integration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple, Union

from custom_components.molly_cody_feeder.const import DOMAIN
from custom_components.molly_cody_feeder.core.env import load_env
from custom_components.molly_cody_feeder.core.paths import DATA_DIR

load_env()


def _parse_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _parse_roi(val: Any) -> Tuple[float, float, float, float]:
    if isinstance(val, (tuple, list)) and len(val) == 4:
        return (float(val[0]), float(val[1]), float(val[2]), float(val[3]))
    try:
        parts = [float(x.strip()) for x in str(val).split(",")]
        if len(parts) == 4:
            return (parts[0], parts[1], parts[2], parts[3])
    except Exception:
        pass
    return (0.10, 0.885, 0.93, 0.995)


def _parse_schedule(val: Any) -> List[str]:
    if isinstance(val, (list, tuple)):
        return [str(t).strip() for t in val if str(t).strip()]
    return [t.strip() for t in str(val).split(",") if t.strip()]


@dataclass
class RuntimeConfig:
    """Configuration for both Home Assistant ConfigEntry runtime and standalone execution."""

    # Storage isolation paths
    storage_dir: Path = field(default_factory=lambda: DATA_DIR)
    session_file: str = field(default_factory=lambda: str(DATA_DIR / "tuya_session.json"))
    captures_dir: Path = field(default_factory=lambda: DATA_DIR / "captures")
    work_dir: Path = field(default_factory=lambda: DATA_DIR / "captures")
    tts_cache_dir: Path = field(default_factory=lambda: DATA_DIR / "tts_cache")

    # Modular compute toggles (F12)
    enable_camera: bool = field(default_factory=lambda: _parse_bool(os.getenv("ENABLE_CAMERA"), True))
    enable_tts: bool = field(default_factory=lambda: _parse_bool(os.getenv("ENABLE_TTS"), True))
    enable_verification: bool = field(default_factory=lambda: _parse_bool(os.getenv("ENABLE_VERIFICATION"), True))
    enable_vision_verification: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("ENABLE_VISION_VERIFICATION"), True)
    )
    enable_mp4_recording: bool = field(default_factory=lambda: _parse_bool(os.getenv("ENABLE_MP4_RECORDING"), False))
    enable_auto_retry: bool = field(default_factory=lambda: _parse_bool(os.getenv("ENABLE_AUTO_RETRY"), True))
    scan_interval: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL", "60")))
    portion_weight_g: int = field(default_factory=lambda: int(os.getenv("PORTION_WEIGHT_G", "10")))

    # Molly & Cody Cloud Account & Feeder Mode
    molly_user: str = field(default_factory=lambda: os.getenv("MOLLY_USER") or os.getenv("USER", ""))
    molly_pass: str = field(default_factory=lambda: os.getenv("MOLLY_PASS") or os.getenv("PASS", ""))
    feeder_mode: str = field(default_factory=lambda: os.getenv("FEEDER_MODE", "cloud").lower())

    # Tuya / Molly & Cody Smart Feeder Device Settings
    tuya_device_id: str = field(default_factory=lambda: os.getenv("TUYA_DEVICE_ID", "yv8xga2javjxtqdc"))
    device_name: str = field(default_factory=lambda: os.getenv("DEVICE_NAME", "M&C Smart Feeder 6L Camera"))
    country_code: str = field(default_factory=lambda: os.getenv("COUNTRY_CODE", "1"))
    feeder_local_ip: str = field(default_factory=lambda: os.getenv("FEEDER_LOCAL_IP", "192.168.1.120"))
    feeder_local_key: str = field(default_factory=lambda: os.getenv("FEEDER_LOCAL_KEY", ""))
    feeder_protocol_version: float = field(
        default_factory=lambda: float(os.getenv("FEEDER_PROTOCOL_VERSION", "3.4"))
    )

    # Camera & Verification Settings
    camera_stream_url: str = field(default_factory=lambda: os.getenv("CAMERA_STREAM_URL", ""))
    bowl_roi: Tuple[float, float, float, float] = field(
        default_factory=lambda: _parse_roi(os.getenv("BOWL_ROI", "0.10,0.885,0.93,0.995"))
    )
    feed_schedule_times: List[str] = field(
        default_factory=lambda: _parse_schedule(os.getenv("FEED_SCHEDULE_TIMES", "07:00,11:00,16:00,19:00"))
    )
    record_duration_seconds: int = field(
        default_factory=lambda: int(os.getenv("RECORD_DURATION_SECONDS", "10"))
    )
    meal_preroll_seconds: int = field(
        default_factory=lambda: int(os.getenv("MEAL_PREROLL_SECONDS", "40"))
    )
    min_kibble_audio_peaks: int = field(
        default_factory=lambda: int(os.getenv("MIN_KIBBLE_AUDIO_PEAKS", "4"))
    )
    ting_rate_min: float = field(
        default_factory=lambda: float(os.getenv("TING_RATE_MIN", "4.3"))
    )
    weak_flow_ting_rate: float = field(
        default_factory=lambda: float(os.getenv("WEAK_FLOW_TING_RATE", "4.6"))
    )
    no_ting_escalate_after: int = field(
        default_factory=lambda: max(1, int(os.getenv("NO_TING_ESCALATE_AFTER", "2")))
    )
    alert_mode: str = field(
        default_factory=lambda: os.getenv("ALERT_MODE", "every_miss").strip().lower()
    )
    no_food_alert_hours: float = field(
        default_factory=lambda: float(os.getenv("NO_FOOD_ALERT_HOURS", "8"))
    )
    food_check_interval_min: int = field(
        default_factory=lambda: int(os.getenv("FOOD_CHECK_INTERVAL_MIN", "60"))
    )
    min_bowl_fill_ratio: float = field(
        default_factory=lambda: float(os.getenv("MIN_BOWL_FILL_RATIO", "0.04"))
    )
    auto_retry_on_clog: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("AUTO_RETRY_ON_CLOG"), True)
    )
    max_retry_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_RETRY_ATTEMPTS", "1"))
    )
    max_daily_retry_portions: int = field(
        default_factory=lambda: int(os.getenv("MAX_DAILY_RETRY_PORTIONS", "3"))
    )
    recording_min_coverage: float = field(
        default_factory=lambda: float(os.getenv("RECORDING_MIN_COVERAGE", "0.75"))
    )
    recording_max_tail_gap_sec: float = field(
        default_factory=lambda: float(os.getenv("RECORDING_MAX_TAIL_GAP_SEC", "5.0"))
    )
    vision_can_override_audio: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("VISION_CAN_OVERRIDE_AUDIO"), False)
    )

    # Hopper, eaten check, daily summary, heartbeat & alert hygiene
    hopper_capacity_portions: int = field(
        default_factory=lambda: int(os.getenv("HOPPER_CAPACITY_PORTIONS", "240"))
    )
    hopper_low_percent: float = field(
        default_factory=lambda: float(os.getenv("HOPPER_LOW_PERCENT", "20"))
    )
    pre_meal_online_check_seconds: int = field(
        default_factory=lambda: int(os.getenv("PRE_MEAL_ONLINE_CHECK_SECONDS", "300"))
    )
    eaten_check_minutes: int = field(
        default_factory=lambda: int(os.getenv("EATEN_CHECK_MINUTES", "120"))
    )
    daily_summary_time: str = field(
        default_factory=lambda: os.getenv("DAILY_SUMMARY_TIME", "21:00").strip()
    )
    heartbeat_url: str = field(
        default_factory=lambda: os.getenv("HEARTBEAT_URL", "").strip()
    )
    alert_cooldown_minutes: float = field(
        default_factory=lambda: float(os.getenv("ALERT_COOLDOWN_MINUTES", "20"))
    )
    ha_notify_on_success: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("HA_NOTIFY_ON_SUCCESS"), True)
    )

    # Two-Way Speaker TTS Alert Settings
    tts_alert_on_clog: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("TTS_ALERT_ON_CLOG"), True)
    )
    tts_alert_message: str = field(
        default_factory=lambda: os.getenv(
            "TTS_ALERT_MESSAGE",
            "Attention! The dog feeder is clogged and no food was dispensed. Please check the feeder bowl.",
        )
    )
    tts_voice: str = field(default_factory=lambda: os.getenv("TTS_VOICE", "en-US-AriaNeural"))
    tts_volume: float = field(default_factory=lambda: float(os.getenv("TTS_VOLUME", "2.5")))
    tts_repeat: int = field(default_factory=lambda: int(os.getenv("TTS_REPEAT", "2")))

    # Optional Gemini Multimodal Vision Verification
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    vision_model: str = field(default_factory=lambda: os.getenv("VISION_MODEL", "gemini-2.5-flash"))

    # Alerting Endpoints
    ha_webhook_url: str = field(default_factory=lambda: os.getenv("HA_WEBHOOK_URL", ""))
    ha_tts_webhook_url: str = field(default_factory=lambda: os.getenv("HA_TTS_WEBHOOK_URL", ""))
    discord_webhook_url: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    ntfy_topic_url: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC_URL", ""))

    # Local Webhook Listener & SQLite Database
    webhook_listen_host: str = field(default_factory=lambda: os.getenv("WEBHOOK_LISTEN_HOST", "127.0.0.1"))
    webhook_listen_port: int = field(default_factory=lambda: int(os.getenv("WEBHOOK_LISTEN_PORT", "8099")))
    webhook_secret: str = field(
        default_factory=lambda: (os.getenv("WEBHOOK_SECRET") or os.getenv("REVIEW_TOKEN", "")).strip()
    )
    db_path: str = field(
        default_factory=lambda: os.getenv("KIBBLE_DB_PATH", str(DATA_DIR / "kibble_history.db"))
    )

    # Storage retention
    keep_footage: bool = field(default_factory=lambda: _parse_bool(os.getenv("KEEP_FOOTAGE"), True))
    capture_retention_days: float = field(
        default_factory=lambda: float(os.getenv("CAPTURE_RETENTION_DAYS", "30"))
    )
    keep_failed_footage_days: float = field(
        default_factory=lambda: float(os.getenv("KEEP_FAILED_FOOTAGE_DAYS", "90"))
    )
    video_keep_days_ok: float = field(
        default_factory=lambda: float(os.getenv("VIDEO_KEEP_DAYS_OK", "0"))
    )
    capture_max_mb: int = field(
        default_factory=lambda: int(os.getenv("CAPTURE_MAX_MB", "2048"))
    )
    db_retention_days: float = field(
        default_factory=lambda: float(os.getenv("DB_RETENTION_DAYS", "0"))
    )
    export_ml_on_prune: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("EXPORT_ML_ON_PRUNE"), True)
    )
    ml_export_path: str = field(
        default_factory=lambda: os.getenv("ML_EXPORT_PATH", str(DATA_DIR / "ml_events.jsonl"))
    )

    def __post_init__(self) -> None:
        if isinstance(self.storage_dir, (str, Path)):
            self.storage_dir = Path(self.storage_dir)
            if self.storage_dir != DATA_DIR:
                self.session_file = str(self.storage_dir / "tuya_session.json")
                self.db_path = str(self.storage_dir / "kibble_history.db")
                self.captures_dir = self.storage_dir / "captures"
                self.work_dir = self.captures_dir
                self.tts_cache_dir = self.storage_dir / "tts_cache"
                self.ml_export_path = str(self.storage_dir / "ml_events.jsonl")

    @property
    def connection_mode(self) -> str:
        return self.feeder_mode

    @connection_mode.setter
    def connection_mode(self, value: str) -> None:
        mode = str(value or "auto").strip().lower()
        self.feeder_mode = "auto" if mode == "hybrid" else mode

    @classmethod
    def from_entry(cls, hass: Any, entry: Any) -> "RuntimeConfig":
        """Constructs an isolated RuntimeConfig from a Home Assistant ConfigEntry (`entry.options` overrides `entry.data`)."""
        entry_id = str(getattr(entry, "entry_id", "default_entry"))
        if hass is not None and hasattr(hass, "config"):
            if hasattr(hass.config, "path") and callable(hass.config.path):
                storage_dir = Path(hass.config.path(".storage", DOMAIN, entry_id))
            elif hasattr(hass.config, "config_dir"):
                storage_dir = Path(str(hass.config.config_dir)) / ".storage" / DOMAIN / entry_id
            else:
                storage_dir = DATA_DIR / entry_id
        else:
            storage_dir = DATA_DIR / entry_id
        return cls.from_mapping(
            data=getattr(entry, "data", None) or {},
            options=getattr(entry, "options", None) or {},
            storage_dir=storage_dir,
        )

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        options: Optional[Mapping[str, Any]] = None,
        storage_dir: Optional[Union[str, Path]] = None,
    ) -> "RuntimeConfig":
        merged: dict[str, Any] = {**dict(data or {}), **dict(options or {})}
        sdir = Path(storage_dir) if storage_dir else DATA_DIR
        sdir.mkdir(parents=True, exist_ok=True)
        captures_dir = sdir / "captures"
        captures_dir.mkdir(parents=True, exist_ok=True)
        tts_cache_dir = sdir / "tts_cache"
        tts_cache_dir.mkdir(parents=True, exist_ok=True)

        raw_mode = str(
            merged.get("connection_mode")
            or merged.get("feeder_mode")
            or "auto"
        ).strip().lower()
        if raw_mode == "hybrid":
            raw_mode = "auto"
        if raw_mode not in ("auto", "cloud", "lan"):
            raw_mode = "auto"

        enable_auto_retry = _parse_bool(
            merged.get("enable_auto_retry", merged.get("auto_retry_on_clog")),
            True,
        )
        enable_tts = _parse_bool(merged.get("enable_tts", True), True)
        tts_alert_on_clog = _parse_bool(merged.get("tts_alert_on_clog", True), True) and enable_tts

        cfg = cls(
            storage_dir=sdir,
            session_file=str(sdir / "tuya_session.json"),
            captures_dir=captures_dir,
            work_dir=captures_dir,
            tts_cache_dir=tts_cache_dir,
            db_path=str(sdir / "kibble_history.db"),
            ml_export_path=str(sdir / "ml_events.jsonl"),
            enable_camera=_parse_bool(merged.get("enable_camera"), True),
            enable_tts=enable_tts,
            enable_verification=_parse_bool(merged.get("enable_verification"), True),
            enable_vision_verification=_parse_bool(merged.get("enable_vision_verification"), True),
            enable_mp4_recording=_parse_bool(merged.get("enable_mp4_recording"), False),
            enable_auto_retry=enable_auto_retry,
            scan_interval=int(merged.get("scan_interval", merged.get("poll_interval_seconds", 60))),
            portion_weight_g=int(merged.get("portion_weight_g", 10)),
            molly_user=str(merged.get("email") or merged.get("molly_user") or ""),
            molly_pass=str(merged.get("password") or merged.get("molly_pass") or ""),
            country_code=str(merged.get("country_code", "1")),
            feeder_mode=raw_mode,
            tuya_device_id=str(merged.get("device_id") or merged.get("tuya_device_id") or "yv8xga2javjxtqdc"),
            device_name=str(merged.get("device_name") or "M&C Smart Feeder 6L Camera"),
            feeder_local_ip=str(merged.get("local_ip") or merged.get("feeder_local_ip") or ""),
            feeder_local_key=str(merged.get("local_key") or merged.get("feeder_local_key") or ""),
            feeder_protocol_version=float(merged.get("protocol_version", merged.get("feeder_protocol_version", 3.4))),
            camera_stream_url=str(merged.get("camera_stream_url", "")),
            bowl_roi=_parse_roi(merged.get("bowl_roi", "0.10,0.885,0.93,0.995")),
            feed_schedule_times=_parse_schedule(merged.get("feed_schedule_times", "07:00,11:00,16:00,19:00")),
            record_duration_seconds=int(merged.get("record_duration_seconds", 10)),
            meal_preroll_seconds=int(merged.get("meal_preroll_seconds", merged.get("meal_preroll_sec", 40))),
            min_kibble_audio_peaks=int(merged.get("min_kibble_audio_peaks", 4)),
            ting_rate_min=float(merged.get("ting_rate_min", 4.3)),
            weak_flow_ting_rate=float(merged.get("weak_flow_ting_rate", 4.6)),
            no_ting_escalate_after=max(1, int(merged.get("no_ting_escalate_after", 2))),
            alert_mode=str(merged.get("alert_mode", "every_miss")).strip().lower(),
            no_food_alert_hours=float(merged.get("no_food_alert_hours", merged.get("no_food_hours", 8.0))),
            food_check_interval_min=int(merged.get("food_check_interval_min", 60)),
            min_bowl_fill_ratio=float(merged.get("min_bowl_fill_ratio", 0.04)),
            auto_retry_on_clog=enable_auto_retry,
            max_retry_attempts=int(merged.get("max_retry_attempts", 1)),
            max_daily_retry_portions=int(merged.get("max_daily_retry_portions", merged.get("max_daily_retry", 3))),
            recording_min_coverage=float(merged.get("recording_min_coverage", 0.75)),
            recording_max_tail_gap_sec=float(merged.get("recording_max_tail_gap_sec", 5.0)),
            vision_can_override_audio=_parse_bool(merged.get("vision_can_override_audio"), False),
            hopper_capacity_portions=int(merged.get("hopper_capacity_portions", merged.get("hopper_capacity", 240))),
            hopper_low_percent=float(merged.get("hopper_low_percent", merged.get("hopper_low_pct", 20.0))),
            pre_meal_online_check_seconds=int(
                merged.get("pre_meal_online_check_seconds", merged.get("pre_meal_online_sec", 300))
            ),
            eaten_check_minutes=int(merged.get("eaten_check_minutes", merged.get("eaten_check_min", 120))),
            daily_summary_time=str(merged.get("daily_summary_time", "21:00")).strip(),
            heartbeat_url=str(merged.get("heartbeat_url", "")).strip(),
            alert_cooldown_minutes=float(merged.get("alert_cooldown_minutes", 20.0)),
            ha_notify_on_success=_parse_bool(merged.get("ha_notify_on_success"), True),
            tts_alert_on_clog=tts_alert_on_clog,
            tts_alert_message=str(
                merged.get(
                    "tts_alert_message",
                    "Attention! The dog feeder is clogged and no food was dispensed. Please check the feeder bowl.",
                )
            ),
            tts_voice=str(merged.get("tts_voice", "en-US-AriaNeural")),
            tts_volume=float(merged.get("tts_volume", 2.5)),
            tts_repeat=int(merged.get("tts_repeat", 2)),
            gemini_api_key=str(merged.get("gemini_api_key", "")),
            vision_model=str(merged.get("vision_model", "gemini-2.5-flash")),
            ha_webhook_url=str(merged.get("ha_webhook_url", "")),
            discord_webhook_url=str(merged.get("discord_webhook_url", "")),
            telegram_bot_token=str(merged.get("telegram_bot_token", "")),
            telegram_chat_id=str(merged.get("telegram_chat_id", "")),
            ntfy_topic_url=str(merged.get("ntfy_topic_url", "")),
            keep_footage=_parse_bool(merged.get("keep_footage"), True),
            capture_retention_days=float(merged.get("capture_retention_days", 30.0)),
            keep_failed_footage_days=float(merged.get("keep_failed_footage_days", 90.0)),
            video_keep_days_ok=float(merged.get("video_keep_days_ok", 0.0)),
            capture_max_mb=int(merged.get("capture_max_mb", 2048)),
            db_retention_days=float(merged.get("db_retention_days", 0.0)),
            export_ml_on_prune=_parse_bool(merged.get("export_ml_on_prune"), True),
        )
        return cfg


WatchdogConfig = RuntimeConfig
