"""Constants for the Molly & Cody Smart Feeder (Poor Puppy Watchdog) integration."""
from __future__ import annotations

from typing import Dict, List, Tuple

DOMAIN: str = "molly_cody_feeder"
NAME: str = "Molly & Cody Smart Feeder"
MANUFACTURER: str = "Molly & Cody (Tuya OEM)"
MODEL: str = "M&C Smart Feeder 6L Camera (yv8xga2javjxtqdc)"
VERSION: str = "0.8.0"

PLATFORMS: List[str] = [
    "sensor",
    "binary_sensor",
    "switch",
    "button",
    "number",
    "select",
    "camera",
    "image",
    "event",
]

# Home Assistant bus events fired on feed verification and alerts
EVENT_FEED: str = "molly_cody_feeder_feed_event"
EVENT_ALERT: str = "molly_cody_feeder_alert_event"

# ---------------------------------------------------------------------------
# Configuration & Options Keys (including standard & short aliases)
# ---------------------------------------------------------------------------
CONF_EMAIL: str = "email"
CONF_PASSWORD: str = "password"
CONF_COUNTRY_CODE: str = "country_code"
CONF_CONNECTION_MODE: str = "connection_mode"
CONF_FEEDER_MODE: str = "feeder_mode"
CONF_DEVICE_ID: str = "device_id"
CONF_DEVICE_NAME: str = "device_name"
CONF_LOCAL_IP: str = "local_ip"
CONF_LOCAL_KEY: str = "local_key"
CONF_PROTOCOL_VERSION: str = "protocol_version"

# Modular compute toggles (F12)
CONF_ENABLE_CAMERA: str = "enable_camera"
CONF_ENABLE_TTS: str = "enable_tts"
CONF_ENABLE_VERIFICATION: str = "enable_verification"
CONF_ENABLE_VISION_VERIFICATION: str = "enable_vision_verification"
CONF_ENABLE_MP4_RECORDING: str = "enable_mp4_recording"
CONF_ENABLE_AUTO_RETRY: str = "enable_auto_retry"

# Polling, hopper, schedule & verification thresholds
CONF_SCAN_INTERVAL: str = "scan_interval"
CONF_POLL_INTERVAL_SECONDS: str = "poll_interval_seconds"
CONF_HOPPER_CAPACITY: str = "hopper_capacity_portions"
CONF_HOPPER_CAPACITY_PORTIONS: str = "hopper_capacity_portions"
CONF_HOPPER_LOW_PCT: str = "hopper_low_percent"
CONF_HOPPER_LOW_PERCENT: str = "hopper_low_percent"
CONF_PORTION_WEIGHT_G: str = "portion_weight_g"

CONF_CAMERA_STREAM_URL: str = "camera_stream_url"
CONF_BOWL_ROI: str = "bowl_roi"
CONF_FEED_SCHEDULE_TIMES: str = "feed_schedule_times"
CONF_RECORD_DURATION_SECONDS: str = "record_duration_seconds"
CONF_MEAL_PREROLL_SEC: str = "meal_preroll_seconds"
CONF_MEAL_PREROLL_SECONDS: str = "meal_preroll_seconds"
CONF_PRE_MEAL_ONLINE_SEC: str = "pre_meal_online_check_seconds"
CONF_PRE_MEAL_ONLINE_CHECK_SECONDS: str = "pre_meal_online_check_seconds"
CONF_EATEN_CHECK_MIN: str = "eaten_check_minutes"
CONF_EATEN_CHECK_MINUTES: str = "eaten_check_minutes"
CONF_DAILY_SUMMARY_TIME: str = "daily_summary_time"
CONF_ALERT_COOLDOWN_MINUTES: str = "alert_cooldown_minutes"
CONF_HA_NOTIFY_ON_SUCCESS: str = "ha_notify_on_success"

CONF_MIN_KIBBLE_AUDIO_PEAKS: str = "min_kibble_audio_peaks"
CONF_TING_RATE_MIN: str = "ting_rate_min"
CONF_WEAK_FLOW_TING_RATE: str = "weak_flow_ting_rate"
CONF_NO_TING_ESCALATE_AFTER: str = "no_ting_escalate_after"
CONF_ALERT_MODE: str = "alert_mode"
CONF_NO_FOOD_HOURS: str = "no_food_alert_hours"
CONF_NO_FOOD_ALERT_HOURS: str = "no_food_alert_hours"
CONF_FOOD_CHECK_INTERVAL_MIN: str = "food_check_interval_min"
CONF_MIN_BOWL_FILL_RATIO: str = "min_bowl_fill_ratio"
CONF_AUTO_RETRY_ON_CLOG: str = "auto_retry_on_clog"
CONF_MAX_RETRY_ATTEMPTS: str = "max_retry_attempts"
CONF_MAX_DAILY_RETRY: str = "max_daily_retry_portions"
CONF_MAX_DAILY_RETRY_PORTIONS: str = "max_daily_retry_portions"
CONF_RECORDING_MIN_COVERAGE: str = "recording_min_coverage"
CONF_RECORDING_MAX_TAIL_GAP_SEC: str = "recording_max_tail_gap_sec"
CONF_VISION_CAN_OVERRIDE_AUDIO: str = "vision_can_override_audio"

CONF_TTS_ALERT_ON_CLOG: str = "tts_alert_on_clog"
CONF_TTS_ALERT_MESSAGE: str = "tts_alert_message"
CONF_TTS_VOICE: str = "tts_voice"
CONF_TTS_VOLUME: str = "tts_volume"
CONF_TTS_REPEAT: str = "tts_repeat"

CONF_KEEP_FOOTAGE: str = "keep_footage"
CONF_CAPTURE_RETENTION_DAYS: str = "capture_retention_days"
CONF_KEEP_FAILED_FOOTAGE_DAYS: str = "keep_failed_footage_days"
CONF_VIDEO_KEEP_DAYS_OK: str = "video_keep_days_ok"
CONF_CAPTURE_MAX_MB: str = "capture_max_mb"
CONF_DB_RETENTION_DAYS: str = "db_retention_days"
CONF_EXPORT_ML_ON_PRUNE: str = "export_ml_on_prune"

CONF_GEMINI_API_KEY: str = "gemini_api_key"
CONF_VISION_MODEL: str = "vision_model"
CONF_DISCORD_WEBHOOK_URL: str = "discord_webhook_url"
CONF_TELEGRAM_BOT_TOKEN: str = "telegram_bot_token"
CONF_TELEGRAM_CHAT_ID: str = "telegram_chat_id"
CONF_NTFY_TOPIC_URL: str = "ntfy_topic_url"
CONF_HEARTBEAT_URL: str = "heartbeat_url"
CONF_HA_WEBHOOK_URL: str = "ha_webhook_url"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_COUNTRY_CODE: str = "1"
DEFAULT_CONNECTION_MODE: str = "auto"
DEFAULT_FEEDER_MODE: str = "cloud"
DEFAULT_PROTOCOL_VERSION: float = 3.4
DEFAULT_BOWL_ROI: Tuple[float, float, float, float] = (0.10, 0.885, 0.93, 0.995)
DEFAULT_BOWL_ROI_STR: str = "0.10,0.885,0.93,0.995"
DEFAULT_FEED_SCHEDULE_TIMES: List[str] = ["07:00", "11:00", "16:00", "19:00"]

DEFAULT_ENABLE_CAMERA: bool = True
DEFAULT_ENABLE_TTS: bool = True
DEFAULT_ENABLE_VERIFICATION: bool = True
DEFAULT_ENABLE_VISION_VERIFICATION: bool = True
DEFAULT_ENABLE_MP4_RECORDING: bool = False
DEFAULT_ENABLE_AUTO_RETRY: bool = True

DEFAULT_SCAN_INTERVAL: int = 60
DEFAULT_POLL_INTERVAL_SECONDS: int = 300
DEFAULT_PORTION_WEIGHT_G: int = 10
DEFAULT_RECORD_DURATION_SECONDS: int = 10
DEFAULT_MEAL_PREROLL_SEC: int = 40
DEFAULT_MEAL_PREROLL_SECONDS: int = 40
DEFAULT_MIN_KIBBLE_AUDIO_PEAKS: int = 4
DEFAULT_TING_RATE_MIN: float = 4.3
DEFAULT_WEAK_FLOW_TING_RATE: float = 4.6
DEFAULT_NO_TING_ESCALATE_AFTER: int = 2
DEFAULT_ALERT_MODE: str = "every_miss"
DEFAULT_NO_FOOD_HOURS: float = 8.0
DEFAULT_NO_FOOD_ALERT_HOURS: float = 8.0
DEFAULT_FOOD_CHECK_INTERVAL_MIN: int = 60
DEFAULT_MIN_BOWL_FILL_RATIO: float = 0.04
DEFAULT_AUTO_RETRY_ON_CLOG: bool = True
DEFAULT_MAX_RETRY_ATTEMPTS: int = 1
DEFAULT_MAX_DAILY_RETRY: int = 3
DEFAULT_MAX_DAILY_RETRY_PORTIONS: int = 3
DEFAULT_RECORDING_MIN_COVERAGE: float = 0.75
DEFAULT_RECORDING_MAX_TAIL_GAP_SEC: float = 5.0
DEFAULT_VISION_CAN_OVERRIDE_AUDIO: bool = False

DEFAULT_HOPPER_CAPACITY: int = 240
DEFAULT_HOPPER_CAPACITY_PORTIONS: int = 240
DEFAULT_HOPPER_LOW_PCT: float = 20.0
DEFAULT_HOPPER_LOW_PERCENT: float = 20.0
DEFAULT_PRE_MEAL_ONLINE_SEC: int = 300
DEFAULT_PRE_MEAL_ONLINE_CHECK_SECONDS: int = 300
DEFAULT_EATEN_CHECK_MIN: int = 120
DEFAULT_EATEN_CHECK_MINUTES: int = 120
DEFAULT_DAILY_SUMMARY_TIME: str = "21:00"
DEFAULT_ALERT_COOLDOWN_MINUTES: float = 20.0
DEFAULT_HA_NOTIFY_ON_SUCCESS: bool = True

DEFAULT_TTS_ALERT_ON_CLOG: bool = True
DEFAULT_TTS_ALERT_MESSAGE: str = (
    "Attention! The dog feeder is clogged and no food was dispensed. Please check the feeder bowl."
)
DEFAULT_TTS_VOICE: str = "en-US-AriaNeural"
DEFAULT_TTS_VOLUME: float = 2.5
DEFAULT_TTS_REPEAT: int = 2

DEFAULT_KEEP_FOOTAGE: bool = True
DEFAULT_CAPTURE_RETENTION_DAYS: float = 30.0
DEFAULT_KEEP_FAILED_FOOTAGE_DAYS: float = 90.0
DEFAULT_VIDEO_KEEP_DAYS_OK: float = 0.0
DEFAULT_CAPTURE_MAX_MB: int = 2048
DEFAULT_DB_RETENTION_DAYS: float = 0.0
DEFAULT_EXPORT_ML_ON_PRUNE: bool = True

DEFAULT_VISION_MODEL: str = "gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Services (all 14 Home Assistant services + maintenance helpers)
# ---------------------------------------------------------------------------
SERVICE_FEED: str = "feed"
SERVICE_VERIFY_FEED: str = "verify_feed"
SERVICE_CHECK_BOWL: str = "check_bowl"
SERVICE_SET_SCHEDULE_SLOT: str = "set_schedule_slot"
SERVICE_SET_SCHEDULE_RAW: str = "set_schedule_raw"
SERVICE_SYNC_SCHEDULES: str = "sync_schedules"
SERVICE_REFILL_HOPPER: str = "refill_hopper"
SERVICE_CAPTURE_SNAPSHOT: str = "capture_snapshot"
SERVICE_PLAY_TTS: str = "play_tts"
SERVICE_SPEAK_TTS: str = "speak_tts"
SERVICE_PLAY_AUDIO: str = "play_audio"
SERVICE_PLAY_CHIME: str = "play_chime"
SERVICE_SET_DP: str = "set_dp"
SERVICE_RUN_ONLINE_CHECK: str = "run_online_check"
SERVICE_CLEAR_CLOG_ALERT: str = "clear_clog_alert"
SERVICE_RECORD_FEEDBACK: str = "record_feedback"
SERVICE_PRUNE_CAPTURES: str = "prune_captures"
SERVICE_RUN_SELFTEST: str = "run_selftest"

ALL_SERVICES: Tuple[str, ...] = (
    SERVICE_FEED,
    SERVICE_VERIFY_FEED,
    SERVICE_CHECK_BOWL,
    SERVICE_SET_SCHEDULE_SLOT,
    SERVICE_SET_SCHEDULE_RAW,
    SERVICE_SYNC_SCHEDULES,
    SERVICE_REFILL_HOPPER,
    SERVICE_CAPTURE_SNAPSHOT,
    SERVICE_PLAY_TTS,
    SERVICE_PLAY_AUDIO,
    SERVICE_PLAY_CHIME,
    SERVICE_SET_DP,
    SERVICE_RUN_ONLINE_CHECK,
    SERVICE_CLEAR_CLOG_ALERT,
)

# ---------------------------------------------------------------------------
# Select / Option Mappings
# ---------------------------------------------------------------------------
FEEDER_MODES: List[str] = ["cloud", "auto", "lan"]
CONNECTION_MODES: List[str] = ["auto", "cloud", "lan"]
ALERT_MODES: List[str] = ["every_miss", "no_food_hours"]
FEEDBACK_VERDICTS: List[str] = ["right", "wrong"]

NIGHTVISION_MODES: Dict[str, str] = {"0": "auto", "1": "off", "2": "on"}
NIGHTVISION_LABELS_TO_DP: Dict[str, str] = {"auto": "0", "off": "1", "on": "2"}

MOTION_SENSITIVITY_MODES: Dict[str, str] = {"0": "low", "1": "medium", "2": "high"}
MOTION_SENSITIVITY_LABELS_TO_DP: Dict[str, str] = {"low": "0", "medium": "1", "high": "2"}
