import os
from dataclasses import dataclass, field
from typing import List, Tuple
from watchdog.env import load_env

load_env()


@dataclass
class WatchdogConfig:
    feeder_mode: str = os.getenv("FEEDER_MODE", "cloud")

    # Molly & Cody / Tuya Cloud Credentials
    molly_user: str = os.getenv("MOLLY_USER") or os.getenv("USER", "")
    molly_pass: str = os.getenv("MOLLY_PASS") or os.getenv("PASS", "")
    user_code: str = os.getenv("USER_CODE", "")
    tuya_region: str = os.getenv("TUYA_REGION", "us")
    tuya_client_id: str = os.getenv("TUYA_CLIENT_ID", "")
    tuya_client_secret: str = os.getenv("TUYA_CLIENT_SECRET", "")
    tuya_device_id: str = os.getenv("TUYA_DEVICE_ID", "")
    tuya_uid: str = os.getenv("TUYA_UID", "")

    # Local Tuya (tinytuya)
    feeder_local_ip: str = os.getenv("FEEDER_LOCAL_IP", "")
    feeder_local_key: str = os.getenv("FEEDER_LOCAL_KEY", "")
    feeder_protocol_version: float = float(os.getenv("FEEDER_PROTOCOL_VERSION", "3.4"))

    # Data Points (DPs) for Molly & Cody Smart Feeder 6L Camera (yv8xga2javjxtqdc)
    dp_manual_feed: str = os.getenv("DP_MANUAL_FEED", "201")
    dp_food_weight: str = os.getenv("DP_FOOD_WEIGHT", "202")
    dp_history: str = os.getenv("DP_HISTORY", "206")
    dp_schedule: str = os.getenv("DP_SCHEDULE", "207")
    dp_fault: str = os.getenv("DP_FAULT", "255")
    dp_feed_report: str = os.getenv("DP_FEED_REPORT", "206")

    # Stream & ROI (in 720x1280 upright portrait orientation, the stainless steel bowl is at the bottom edge)
    camera_stream_url: str = os.getenv("CAMERA_STREAM_URL", "")
    bowl_roi: Tuple[float, float, float, float] = field(
        default_factory=lambda: _parse_roi(os.getenv("BOWL_ROI", "0.10,0.885,0.93,0.995"))
    )
    db_path: str = os.getenv("DB_PATH", "kibble_history.db")
    # Retention & ML export (see watchdog/service/retention.py)
    keep_footage: bool = os.getenv("KEEP_FOOTAGE", "true").lower() in ("1", "true", "yes")
    capture_retention_days: float = float(os.getenv("CAPTURE_RETENTION_DAYS", "30") or 0)
    keep_failed_footage_days: float = float(
        os.getenv("KEEP_FAILED_FOOTAGE_DAYS") or os.getenv("CAPTURE_RETENTION_DAYS", "30") or 0
    )
    video_keep_days_ok: float = float(os.getenv("VIDEO_KEEP_DAYS_OK", "0") or 0)
    capture_max_mb: int = int(os.getenv("CAPTURE_MAX_MB", "2048") or 0)
    db_retention_days: float = float(os.getenv("DB_RETENTION_DAYS", "0") or 0)
    export_ml_on_prune: bool = os.getenv("EXPORT_ML_ON_PRUNE", "true").lower() in ("1", "true", "yes")
    ml_export_path: str = os.getenv("ML_EXPORT_PATH", "data/ml_events.jsonl")

    # Verification parameters
    feed_schedule_times: List[str] = field(
        default_factory=lambda: [
            t.strip() for t in os.getenv("FEED_SCHEDULE_TIMES", "07:00,11:00,16:00,19:00").split(",") if t.strip()
        ]
    )
    record_duration_seconds: int = int(os.getenv("RECORD_DURATION_SECONDS", "10"))
    # Scheduled meals: start watching this many seconds BEFORE the meal time (dispense timing drifts)
    meal_preroll_seconds: int = int(os.getenv("MEAL_PREROLL_SECONDS", "40"))
    min_kibble_audio_peaks: int = int(os.getenv("MIN_KIBBLE_AUDIO_PEAKS", "4"))
    # Kibble "ting" rate (impacts with the pellet-on-steel signature per motor second) that means food came out
    ting_rate_min: float = float(os.getenv("TING_RATE_MIN", "4.3"))
    # This many feeds in a row without the ting => urgent clog / no-food alert (1 miss = heads-up only)
    no_ting_escalate_after: int = int(os.getenv("NO_TING_ESCALATE_AFTER", "2"))
    # Alert policy: "every_miss" (per-feed ting rules) or "no_food_hours" (ONLY alert when no evidence of
    # food — a feed with the kibble ting, or food seen in a bowl snapshot — for NO_FOOD_ALERT_HOURS)
    alert_mode: str = os.getenv("ALERT_MODE", "every_miss").strip().lower()
    no_food_alert_hours: float = float(os.getenv("NO_FOOD_ALERT_HOURS", "8"))
    food_check_interval_min: int = int(os.getenv("FOOD_CHECK_INTERVAL_MIN", "60"))
    min_bowl_fill_ratio: float = float(os.getenv("MIN_BOWL_FILL_RATIO", "0.04"))
    auto_retry_on_clog: bool = os.getenv("AUTO_RETRY_ON_CLOG", "true").lower() in ("1", "true", "yes")
    max_retry_attempts: int = int(os.getenv("MAX_RETRY_ATTEMPTS", "1"))
    # Overfeed guard: max portions the watchdog may auto-dispense per day via retries
    max_daily_retry_portions: int = int(os.getenv("MAX_DAILY_RETRY_PORTIONS", "3"))
    # Recording coverage: a recording is "cut short" (camera stream dropped) when the audio received
    # covers less than this share of the recording window, or the last audio arrived more than
    # RECORDING_MAX_TAIL_GAP_SEC before the end. Cut short + no kibble heard = UNVERIFIED, never a clog.
    recording_min_coverage: float = float(os.getenv("RECORDING_MIN_COVERAGE", "0.75"))
    recording_max_tail_gap_sec: float = float(os.getenv("RECORDING_MAX_TAIL_GAP_SEC", "5"))
    # Audio is the reliable signal. When the audio says "no kibble", vision (IR / dog detectors are noisy)
    # must not flip the verdict to "fed" — set true to restore the old "vision can confirm" behaviour.
    vision_can_override_audio: bool = os.getenv("VISION_CAN_OVERRIDE_AUDIO", "false").lower() in ("1", "true", "yes")
    # Soft "weak kibble flow" warning below this many kibble tings per motor-second (v0.6: all hand-labelled
    # partial jams were 3.2-4.6/s, normal feeds mostly 4.6-8.4/s). The feed still counts as fed.
    weak_flow_ting_rate: float = float(os.getenv("WEAK_FLOW_TING_RATE", "4.6"))

    # Vision LLM (optional secondary check)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    vision_model: str = os.getenv("VISION_MODEL", "gemini-2.5-flash")

    # Home Assistant Webhooks (Outbound & Inbound)
    ha_webhook_url: str = os.getenv("HA_WEBHOOK_URL", "")
    ha_notify_on_success: bool = os.getenv("HA_NOTIFY_ON_SUCCESS", "true").lower() in ("1", "true", "yes")
    webhook_listen_host: str = os.getenv("WEBHOOK_LISTEN_HOST", "127.0.0.1")
    webhook_listen_port: int = int(os.getenv("WEBHOOK_LISTEN_PORT", "8080"))
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    # Additional Alerting Channels
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    ntfy_topic_url: str = os.getenv("NTFY_TOPIC_URL", "")
    # Same alert event is not re-sent within this window (prevents notification storms)
    alert_cooldown_minutes: float = float(os.getenv("ALERT_COOLDOWN_MINUTES", "20"))
    # Dead-man switch URL pinged every 5 minutes (e.g. https://hc-ping.com/<uuid>)
    heartbeat_url: str = os.getenv("HEARTBEAT_URL", "")
    # Daily "all good / problems today" summary push at this time (HH:MM, blank = off)
    daily_summary_time: str = os.getenv("DAILY_SUMMARY_TIME", "21:00")
    # Hopper estimate: portions a full hopper holds, and the low-level warning threshold
    hopper_capacity_portions: int = int(os.getenv("HOPPER_CAPACITY_PORTIONS", "240"))
    hopper_low_percent: float = float(os.getenv("HOPPER_LOW_PERCENT", "20"))
    # Check the feeder is online this many seconds before each meal (0 = off)
    pre_meal_online_check_seconds: int = int(os.getenv("PRE_MEAL_ONLINE_CHECK_SECONDS", "300"))
    # Health: re-check the bowl this many minutes after a verified meal (daylight only, 0 = off)
    eaten_check_minutes: int = int(os.getenv("EATEN_CHECK_MINUTES", "120"))

    # Built-in Feeder Speaker TTS Alerting (100% Cloud Two-Way Talkback)
    tts_alert_on_clog: bool = os.getenv("TTS_ALERT_ON_CLOG", "true").lower() in ("1", "true", "yes")
    tts_alert_message: str = os.getenv(
        "TTS_ALERT_MESSAGE",
        "Attention! The dog feeder is clogged and no food was dispensed. Please check the feeder bowl.",
    )
    tts_voice: str = os.getenv("TTS_VOICE", "en-US-AriaNeural")
    tts_volume: float = float(os.getenv("TTS_VOLUME", "2.5"))
    tts_repeat: int = int(os.getenv("TTS_REPEAT", "2"))


def _parse_roi(raw: str) -> Tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in raw.split(",")]
    if len(parts) != 4:
        return (0.78, 0.10, 0.99, 0.92)
    return (parts[0], parts[1], parts[2], parts[3])
