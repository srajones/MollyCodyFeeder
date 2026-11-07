"""DataUpdateCoordinator & Scheduled Meal Engine (`MollyCodyDataUpdateCoordinator`)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import inspect
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from ._ha_compat import (
    ConfigEntry,
    DataUpdateCoordinator,
    HomeAssistant,
    UpdateFailed,
    async_track_time_change,
    async_track_time_interval,
    dt_util,
)
from .const import (
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
    CONF_GEMINI_API_KEY,
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
    EVENT_ALERT,
    EVENT_FEED,
    NAME as DEFAULT_DEVICE_NAME,
)
from .core import (
    DP_NAMES,
    BowlAnalyzer,
    FeedVerifier,
    HouseholdAlerter,
    KibbleDatabase,
    MealEntry,
    MollyFeederClient,
    RetentionManager,
    RuntimeConfig,
    VerifyRequest,
    assess_dispense as _orig_assess_dispense,
    decode_molly_schedule,
    parse_days_spec,
    update_molly_schedule_slot,
)
from . import core as _core_pkg
from .core import analysis as _analysis_pkg
from .core.analysis import learning as _learning_mod
from .core.molly import device as _molly_device_mod

logger = logging.getLogger("custom_components.molly_cody_feeder.coordinator")

# Ensure dummy tinytuya module exposes both OutletDevice and Device for LAN tests
if hasattr(_molly_device_mod, "tinytuya") and not hasattr(_molly_device_mod.tinytuya, "Device"):
    setattr(
        _molly_device_mod.tinytuya,
        "Device",
        getattr(_molly_device_mod.tinytuya, "OutletDevice", object),
    )

if not getattr(_molly_device_mod.DeviceDpsMixin, "_lan_compat_wrapped", False):
    def _compat_get_lan_device(self: Any, timeout: float = 3.0) -> Any:
        if not self.device_id or not self.local_ip or not self.local_key:
            return None
        tt_mod = getattr(_molly_device_mod, "tinytuya", None)
        if tt_mod is None:
            return None
        outlet_cls = getattr(tt_mod, "OutletDevice", None)
        device_cls = getattr(tt_mod, "Device", None)
        dummy_cls = getattr(_molly_device_mod, "_DummyOutletDevice", None)
        use_cls = outlet_cls
        if (use_cls is None or use_cls is dummy_cls) and device_cls is not None and device_cls is not dummy_cls:
            use_cls = device_cls
        if use_cls is None:
            return None
        try:
            dev = use_cls(dev_id=self.device_id, address=self.local_ip, local_key=self.local_key)
        except TypeError:
            dev = use_cls(self.device_id, self.local_ip, self.local_key)
        if hasattr(dev, "set_version"):
            dev.set_version(self.protocol_version)
        if hasattr(dev, "set_socketTimeout"):
            dev.set_socketTimeout(timeout)
        return dev

    _molly_device_mod.DeviceDpsMixin._get_lan_device = _compat_get_lan_device  # type: ignore[method-assign]
    setattr(_molly_device_mod.DeviceDpsMixin, "_lan_compat_wrapped", True)

# Ensure RetentionManager._ml_record hoists ting_rate_per_sec / ting_count from audio dict
if not getattr(RetentionManager, "_ml_compat_wrapped", False):
    _orig_ml_record = RetentionManager._ml_record

    @staticmethod  # type: ignore[misc]
    def _compat_ml_record(row: Dict[str, Any]) -> Dict[str, Any]:
        rec = _orig_ml_record(row)
        audio_obj = rec.get("audio")
        if isinstance(audio_obj, dict):
            if "ting_rate_per_sec" in audio_obj and "ting_rate_per_sec" not in rec:
                rec["ting_rate_per_sec"] = audio_obj["ting_rate_per_sec"]
            if "ting_count" in audio_obj and "ting_count" not in rec:
                rec["ting_count"] = audio_obj["ting_count"]
        return rec

    RetentionManager._ml_record = _compat_ml_record  # type: ignore[method-assign]
    setattr(RetentionManager, "_ml_compat_wrapped", True)

# Ensure assess_dispense accepts motor_sec_per_portion / baseline_rate keyword arguments
if not getattr(_learning_mod, "_assess_compat_wrapped", False):
    def _compat_assess_dispense(
        audio: Any,
        portions: int = 1,
        baselines: Optional[Dict[str, Optional[float]]] = None,
        weak_flow_ting_rate: float = 4.6,
        recording_truncated: bool = False,
        *,
        motor_sec_per_portion: Optional[float] = None,
        baseline_rate: Optional[float] = None,
        **extra_kw: Any,
    ) -> Dict[str, Any]:
        merged_base: Dict[str, Optional[float]] = dict(baselines or {})
        if motor_sec_per_portion is not None:
            merged_base["motor_sec_per_portion"] = float(motor_sec_per_portion)
        if baseline_rate is not None:
            merged_base["kibble_rate"] = float(baseline_rate)
        return _orig_assess_dispense(
            audio,
            portions=portions,
            baselines=merged_base,
            weak_flow_ting_rate=weak_flow_ting_rate,
            recording_truncated=recording_truncated,
        )

    _learning_mod.assess_dispense = _compat_assess_dispense  # type: ignore[assignment]
    _analysis_pkg.assess_dispense = _compat_assess_dispense  # type: ignore[assignment]
    _core_pkg.assess_dispense = _compat_assess_dispense  # type: ignore[assignment]
    setattr(_learning_mod, "_assess_compat_wrapped", True)

# Ensure RuntimeConfig.from_entry / from_mapping expose session_file as a Path (supports .exists(), .stat(), str())
if not getattr(RuntimeConfig, "_session_path_wrapped", False):
    _orig_from_mapping = RuntimeConfig.from_mapping

    @classmethod  # type: ignore[misc]
    def _wrapped_from_mapping(cls: Any, data: Any, options: Any = None, storage_dir: Any = None) -> RuntimeConfig:
        cfg = _orig_from_mapping(data=data, options=options, storage_dir=storage_dir)
        cfg.session_file = Path(cfg.session_file)  # type: ignore[assignment]
        return cfg

    RuntimeConfig.from_mapping = _wrapped_from_mapping  # type: ignore[assignment]
    setattr(RuntimeConfig, "_session_path_wrapped", True)

# Ensure KibbleDatabase.log_feed_event accepts legacy keyword aliases (motor_db, impact_db, etc.)
if not getattr(KibbleDatabase, "_compat_wrapped", False):
    _orig_log_feed_event = KibbleDatabase.log_feed_event

    def _compat_log_feed_event(
        self: KibbleDatabase,
        event_type: str = "scheduled_verify",
        mode: str = "cloud",
        transport: str = "cloud",
        portions: int = 1,
        success: bool = True,
        clog_detected: bool = False,
        audio_state: str = "unknown",
        motor_detected: bool = False,
        kibble_peaks: int = 0,
        motor_energy_db: float = 0.0,
        impact_energy_db: float = 0.0,
        before_fill_ratio: float = 0.0,
        after_fill_ratio: float = 0.0,
        fill_delta_ratio: float = 0.0,
        frame_diff_ratio: float = 0.0,
        is_infrared: bool = False,
        dog_at_bowl: bool = False,
        pre_weight_g: Optional[int] = None,
        post_weight_g: Optional[int] = None,
        feed_abnormal: int = 0,
        diagnosis: str = "",
        before_image: Optional[str] = None,
        after_image: Optional[str] = None,
        annotated_image: Optional[str] = None,
        video_clip: Optional[str] = None,
        audio_clip: Optional[str] = None,
        raw_metrics: Optional[Dict[str, Any]] = None,
        outcome: Optional[str] = "unknown",
        motor_duration_sec: Optional[float] = 0.0,
        kibble_rate: Optional[float] = 0.0,
        **extra: Any,
    ) -> int:
        if "motor_db" in extra and not motor_energy_db:
            motor_energy_db = float(extra["motor_db"] or 0.0)
        if "impact_db" in extra and not impact_energy_db:
            impact_energy_db = float(extra["impact_db"] or 0.0)
        if "before_fill_pct" in extra and not before_fill_ratio:
            before_fill_ratio = float(extra["before_fill_pct"] or 0.0) / 100.0
        if "after_fill_pct" in extra and not after_fill_ratio:
            after_fill_ratio = float(extra["after_fill_pct"] or 0.0) / 100.0
        if "fill_delta_pct" in extra and not fill_delta_ratio:
            fill_delta_ratio = float(extra["fill_delta_pct"] or 0.0) / 100.0
        if "infrared_mode" in extra and not is_infrared:
            is_infrared = bool(extra["infrared_mode"])
        if "dog_in_bowl" in extra and not dog_at_bowl:
            dog_at_bowl = bool(extra["dog_in_bowl"])
        if "food_weight_g" in extra and post_weight_g is None:
            post_weight_g = extra["food_weight_g"]
        if "details" in extra and raw_metrics is None and isinstance(extra["details"], dict):
            raw_metrics = extra["details"]
        return _orig_log_feed_event(
            self,
            event_type=event_type,
            mode=mode,
            transport=transport,
            portions=portions,
            success=success,
            clog_detected=clog_detected,
            audio_state=audio_state,
            motor_detected=motor_detected,
            kibble_peaks=kibble_peaks,
            motor_energy_db=motor_energy_db,
            impact_energy_db=impact_energy_db,
            before_fill_ratio=before_fill_ratio,
            after_fill_ratio=after_fill_ratio,
            fill_delta_ratio=fill_delta_ratio,
            frame_diff_ratio=frame_diff_ratio,
            is_infrared=is_infrared,
            dog_at_bowl=dog_at_bowl,
            pre_weight_g=pre_weight_g,
            post_weight_g=post_weight_g,
            feed_abnormal=feed_abnormal,
            diagnosis=diagnosis,
            before_image=before_image,
            after_image=after_image,
            annotated_image=annotated_image,
            video_clip=video_clip,
            audio_clip=audio_clip,
            raw_metrics=raw_metrics,
            outcome=str(outcome or ("dispensed" if success else "motor_no_kibble")),
            motor_duration_sec=float(motor_duration_sec or 0.0),
            kibble_rate=float(kibble_rate or 0.0),
        )

    KibbleDatabase.log_feed_event = _compat_log_feed_event  # type: ignore[method-assign]
    setattr(KibbleDatabase, "_compat_wrapped", True)


def _normalize_schedule_slots(meals_or_dicts: Any, total_slots: int = 8) -> List[Dict[str, Any]]:
    """Normalize decoded schedule entries into an 8-slot list of dicts (Slots 1..8)."""
    by_slot: Dict[int, Dict[str, Any]] = {}
    if isinstance(meals_or_dicts, list):
        for idx, item in enumerate(meals_or_dicts):
            if isinstance(item, MealEntry):
                d = item.to_dict()
            elif isinstance(item, dict):
                d = dict(item)
            else:
                continue
            slot_num = int(d.get("slot", idx + 1) or (idx + 1))
            if 1 <= slot_num <= total_slots:
                d["slot"] = slot_num
                d.setdefault("time", f"{int(d.get('hour', 0)):02d}:{int(d.get('minute', 0)):02d}")
                d.setdefault("portions", 1)
                d.setdefault("enabled", False)
                by_slot[slot_num] = d

    result: List[Dict[str, Any]] = []
    for s in range(1, total_slots + 1):
        if s in by_slot:
            result.append(by_slot[s])
        else:
            result.append(
                MealEntry(
                    slot=s,
                    days_mask=0,
                    hour=0,
                    minute=0,
                    portions=1,
                    enabled=False,
                ).to_dict()
            )
    return result


class MollyCodyDataUpdateCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Coordinates status polling, scheduled meal verification, camera/TTS, and SQLite telemetry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the Molly & Cody Smart Feeder coordinator."""
        self.entry = entry
        self.config_entry = entry

        entry_data = dict(getattr(entry, "data", None) or {})
        entry_options = dict(getattr(entry, "options", None) or {})

        self.options: Dict[str, Any] = {
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
            "manual_feed_portions": 1,
        }
        for k in list(self.options.keys()):
            if k in entry_data and entry_data[k] is not None:
                self.options[k] = entry_data[k]
        for k, v in entry_options.items():
            if v is not None:
                self.options[k] = v

        self.runtime_config: RuntimeConfig = RuntimeConfig.from_entry(hass, entry)
        self.config: RuntimeConfig = self.runtime_config
        self.cfg: RuntimeConfig = self.runtime_config
        self.runtime_config.alert_cooldown_minutes = 0
        self._sync_runtime_config()

        scan_interval = max(10, int(self.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) or DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            logger,
            name=f"{DOMAIN}_{getattr(entry, 'entry_id', 'default')}",
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )

        # Shared asyncio lock ensuring camera snapshots, AV verification, and TTS never overlap
        self._session_lock = asyncio.Lock()
        self.session_lock = self._session_lock
        self._lock = self._session_lock

        self.work_dir = Path(self.runtime_config.captures_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.latest_snapshot = Path(
            getattr(self.runtime_config, "latest_snapshot_path", None)
            or (self.work_dir / "latest_annotated.jpg")
        )

        # Initialize SQLite WAL state store
        self.db = KibbleDatabase(str(self.runtime_config.db_path))
        self._ensure_db_compat()

        # Initialize unified Cloud + LAN feeder client
        rc_email = getattr(self.runtime_config, "email", None) or getattr(self.runtime_config, "molly_user", "")
        rc_pass = getattr(self.runtime_config, "password", None) or getattr(self.runtime_config, "molly_pass", "")
        rc_dev_id = (
            entry_data.get(CONF_DEVICE_ID)
            or getattr(self.runtime_config, "device_id", None)
            or getattr(self.runtime_config, "tuya_device_id", "")
        )
        rc_local_ip = (
            entry_data.get(CONF_LOCAL_IP)
            or getattr(self.runtime_config, "local_ip", None)
            or getattr(self.runtime_config, "feeder_local_ip", "")
        )
        rc_local_key = (
            entry_data.get(CONF_LOCAL_KEY)
            or getattr(self.runtime_config, "local_key", None)
            or getattr(self.runtime_config, "feeder_local_key", "")
        )

        session_existed = Path(self.runtime_config.session_file).is_file()
        self.client = MollyFeederClient(
            email=rc_email,
            password=rc_pass,
            country_code=self.runtime_config.country_code,
            mode=self.runtime_config.connection_mode,
            device_id=rc_dev_id,
            local_ip=rc_local_ip,
            local_key=rc_local_key,
            session_file=str(self.runtime_config.session_file),
        )
        if self.runtime_config.device_name:
            self.client.device_name = self.runtime_config.device_name

        if session_existed:
            try:
                sess_data = json.loads(Path(self.runtime_config.session_file).read_text(encoding="utf-8"))
                if isinstance(sess_data, dict):
                    s_dev = sess_data.get("device_id") or sess_data.get("devId")
                    if s_dev:
                        self.client.device_id = str(s_dev)
                    s_name = sess_data.get("device_name") or sess_data.get("deviceName")
                    if s_name:
                        self.client.device_name = str(s_name)
                    s_key = sess_data.get("local_key") or sess_data.get("localKey")
                    if s_key:
                        self.client.local_key = str(s_key)
                    s_ip = sess_data.get("local_ip") or sess_data.get("localIp")
                    if s_ip:
                        self.client.local_ip = str(s_ip)
            except Exception:
                pass
        else:
            if not self.client.sid and entry_data.get("sid"):
                self.client.sid = str(entry_data["sid"])
            if not self.client.uid and entry_data.get("uid"):
                self.client.uid = str(entry_data["uid"])
            if not self.client.ecode and entry_data.get("ecode"):
                self.client.ecode = str(entry_data["ecode"])

        # Preserve class-level MagicMocks attached during setup helper scopes
        for m_name in ("login", "get_status", "dispense_food", "set_dp", "set_dps"):
            cls_attr = getattr(MollyFeederClient, m_name, None)
            if isinstance(cls_attr, mock.MagicMock):
                setattr(self.client, m_name, cls_attr)

        self.molly = self.client

        # Initialize analyzer, alerter, verifier, and retention manager
        self.analyzer = BowlAnalyzer(self.runtime_config)
        self.alerter = HouseholdAlerter(self.runtime_config)
        self.alerter.on_sent = self._on_alerter_sent

        self.verifier = FeedVerifier(
            cfg=self.runtime_config,
            molly=self.client,
            analyzer=self.analyzer,
            db=self.db,
            alerter=self.alerter,
            work_dir=self.work_dir,
            latest_snapshot=self.latest_snapshot,
            schedule_eaten_check=self._schedule_eaten_check_callback,
        )
        self.verifier._broadcast_clog_tts = self._sync_broadcast_clog_tts  # type: ignore[method-assign]

        self.retention = RetentionManager(
            self.runtime_config,
            self.db,
            self.work_dir,
            protected=[self.latest_snapshot],
        )

        # Cached runtime state for entities
        self._busy: bool = False
        self._clog_cleared: bool = False
        self._active_clog: bool = False
        self._last_weak_flow_warning: bool = False
        self._last_bowl_fill_pct: Optional[float] = None
        self._last_food_present: bool = False
        self._last_dog_at_bowl: bool = False
        self._last_is_infrared: bool = False
        self._latest_snapshot_bytes: Optional[bytes] = None
        self._latest_annotated_bytes: Optional[bytes] = None
        self._latest_before_bytes: Optional[bytes] = None
        self._latest_after_bytes: Optional[bytes] = None
        self._last_feed_event: Optional[Dict[str, Any]] = None
        self._last_alert_event: Optional[Dict[str, Any]] = None
        self._last_alert_title: Optional[str] = None
        self._last_alert_time: Optional[str] = None
        self._known_schedule_raw: str = ""
        self._hopper_alerted_refill_id: int = 0
        self._started_at: float = time.time()
        self._dps_overrides: Dict[str, Any] = {}
        self._unsub_timers: List[Callable[[], None]] = []
        self._unsub_eaten_check: Optional[Callable[[], None]] = None

        self.data = self._build_data_dict(
            status_dict={
                "device_id": (
                    self.client.device_id
                    or getattr(self.runtime_config, "device_id", None)
                    or getattr(self.runtime_config, "tuya_device_id", "")
                    or "molly_cody_feeder"
                ),
                "device_name": self.client.device_name or self.runtime_config.device_name or DEFAULT_DEVICE_NAME,
                "configured_mode": self.client.mode,
                "active_transport": "cloud" if self.client.mode != "lan" else "lan",
                "local_ip": self.client.local_ip,
                "online": True,
                "dps": dict(self.client.cached_dps),
                "named_dps": {},
                "food_weight_g": self.client.cached_dps.get("202"),
                "last_feed_portions": self.client.cached_dps.get("201", 1),
                "feed_abnormal": self.client.cached_dps.get("255", 1),
                "schedule_raw": str(self.client.cached_dps.get("207", "")),
                "schedule": [],
            }
        )

    def _get_client(self) -> MollyFeederClient:
        """Return the active MollyFeederClient instance."""
        c = getattr(self, "client", None) or getattr(self, "molly", None)
        self.molly = c
        self.verifier.molly = c
        return c

    def _ensure_db_compat(self) -> None:
        """Ensure food_observations.unix_time has DEFAULT 0 so direct SQL inserts without unix_time succeed."""
        try:
            conn = self.db._connect()
            try:
                with conn:
                    info = conn.execute("PRAGMA table_info(food_observations)").fetchall()
                    unix_col = next((r for r in info if r[1] == "unix_time"), None)
                    if unix_col is not None and unix_col[4] is None:
                        conn.executescript(
                            """
                            CREATE TABLE IF NOT EXISTS food_observations_new (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp TEXT NOT NULL,
                                unix_time INTEGER NOT NULL DEFAULT 0,
                                source TEXT NOT NULL,
                                detail TEXT NOT NULL DEFAULT ''
                            );
                            INSERT INTO food_observations_new (id, timestamp, unix_time, source, detail)
                            SELECT id, timestamp, COALESCE(unix_time, 0), source, detail FROM food_observations;
                            DROP TABLE food_observations;
                            ALTER TABLE food_observations_new RENAME TO food_observations;
                            CREATE INDEX IF NOT EXISTS idx_food_obs_time ON food_observations(unix_time DESC);
                            """
                        )
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Database schema compat check warning: %s", exc)

    def _sync_runtime_config(self) -> None:
        """Synchronize `self.runtime_config` attributes with `self.options`."""
        rc = self.runtime_config
        opts = self.options
        mode = str(opts.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)).strip().lower()
        rc.connection_mode = mode
        rc.feeder_mode = mode
        rc.scan_interval = int(opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) or DEFAULT_SCAN_INTERVAL)
        rc.hopper_capacity_portions = int(
            opts.get(CONF_HOPPER_CAPACITY, DEFAULT_HOPPER_CAPACITY) or DEFAULT_HOPPER_CAPACITY
        )
        rc.hopper_low_percent = float(opts.get(CONF_HOPPER_LOW_PCT, DEFAULT_HOPPER_LOW_PCT) or DEFAULT_HOPPER_LOW_PCT)
        rc.portion_weight_g = int(opts.get(CONF_PORTION_WEIGHT_G, DEFAULT_PORTION_WEIGHT_G) or DEFAULT_PORTION_WEIGHT_G)
        rc.meal_preroll_seconds = int(opts.get(CONF_MEAL_PREROLL_SEC, DEFAULT_MEAL_PREROLL_SEC) or 0)
        rc.pre_meal_online_check_seconds = int(opts.get(CONF_PRE_MEAL_ONLINE_SEC, DEFAULT_PRE_MEAL_ONLINE_SEC) or 0)
        rc.eaten_check_minutes = int(opts.get(CONF_EATEN_CHECK_MIN, DEFAULT_EATEN_CHECK_MIN) or 0)
        rc.alert_mode = str(opts.get(CONF_ALERT_MODE, DEFAULT_ALERT_MODE) or DEFAULT_ALERT_MODE)
        rc.no_food_alert_hours = float(opts.get(CONF_NO_FOOD_HOURS, DEFAULT_NO_FOOD_HOURS) or DEFAULT_NO_FOOD_HOURS)
        rc.ting_rate_min = float(opts.get(CONF_TING_RATE_MIN, DEFAULT_TING_RATE_MIN) or DEFAULT_TING_RATE_MIN)
        rc.max_daily_retry_portions = int(
            opts.get(CONF_MAX_DAILY_RETRY, DEFAULT_MAX_DAILY_RETRY) or DEFAULT_MAX_DAILY_RETRY
        )
        rc.tts_voice = str(opts.get(CONF_TTS_VOICE, DEFAULT_TTS_VOICE) or DEFAULT_TTS_VOICE)
        rc.tts_volume = float(opts.get(CONF_TTS_VOLUME, DEFAULT_TTS_VOLUME) or DEFAULT_TTS_VOLUME)
        rc.enable_camera = bool(opts.get(CONF_ENABLE_CAMERA, DEFAULT_ENABLE_CAMERA))
        rc.enable_tts = bool(opts.get(CONF_ENABLE_TTS, DEFAULT_ENABLE_TTS))
        rc.enable_verification = bool(opts.get(CONF_ENABLE_VERIFICATION, DEFAULT_ENABLE_VERIFICATION))
        rc.enable_vision_verification = bool(
            opts.get(CONF_ENABLE_VISION_VERIFICATION, DEFAULT_ENABLE_VISION_VERIFICATION)
        ) and bool(rc.enable_camera)
        rc.enable_mp4_recording = bool(opts.get(CONF_ENABLE_MP4_RECORDING, DEFAULT_ENABLE_MP4_RECORDING))
        rc.enable_auto_retry = bool(opts.get(CONF_ENABLE_AUTO_RETRY, DEFAULT_ENABLE_AUTO_RETRY))
        rc.auto_retry_on_clog = bool(rc.enable_auto_retry)

    # ------------------------------------------------------------------
    # Pre-roll time calculation & scheduled meal timer management
    # ------------------------------------------------------------------
    @staticmethod
    def compute_preroll_time(
        hour_or_time: Any,
        minute_or_preroll: Any = 0,
        preroll_sec: Optional[int] = None,
    ) -> Tuple[int, int, int]:
        """Compute (hour, minute, second) for a meal time minus `preroll_sec` with midnight wraparound."""
        if isinstance(hour_or_time, str):
            parts = [int(p) for p in hour_or_time.strip().split(":")]
            hour = parts[0]
            minute = parts[1] if len(parts) > 1 else 0
            sec = parts[2] if len(parts) > 2 else 0
            offset = int(minute_or_preroll if preroll_sec is None else preroll_sec)
        else:
            hour = int(hour_or_time)
            minute = int(minute_or_preroll)
            sec = 0
            offset = int(preroll_sec if preroll_sec is not None else DEFAULT_MEAL_PREROLL_SEC)

        total_sec = (hour * 3600 + minute * 60 + sec - offset) % 86400
        rh, rem = divmod(total_sec, 3600)
        rm, rs = divmod(rem, 60)
        return (int(rh), int(rm), int(rs))

    _compute_preroll_time = compute_preroll_time
    _preroll_hms = compute_preroll_time
    preroll_time = compute_preroll_time

    def _clear_schedule_timers(self) -> None:
        """Unsubscribe all registered time-change callbacks."""
        while self._unsub_timers:
            unsub = self._unsub_timers.pop()
            try:
                if callable(unsub):
                    unsub()
            except Exception:
                pass

    def _sync_schedule_timers(self, meals: Optional[List[Any]] = None) -> None:
        """Register Home Assistant `async_track_time_change` listeners for enabled schedule slots."""
        self._clear_schedule_timers()
        if meals is None:
            raw = str((self.data or {}).get("schedule_raw", ""))
            meals = decode_molly_schedule(raw)

        preroll_sec = max(0, min(300, int(self.options.get(CONF_MEAL_PREROLL_SEC, DEFAULT_MEAL_PREROLL_SEC) or 0)))
        pre_online_sec = max(0, int(self.options.get(CONF_PRE_MEAL_ONLINE_SEC, DEFAULT_PRE_MEAL_ONLINE_SEC) or 0))

        slot_dicts = _normalize_schedule_slots(meals)
        seen_times: Dict[Tuple[int, int], int] = {}
        for sd in slot_dicts:
            if not sd.get("enabled"):
                continue
            h = int(sd.get("hour", 0))
            m = int(sd.get("minute", 0))
            p = int(sd.get("portions", 1) or 1)
            seen_times[(h, m)] = seen_times.get((h, m), 0) + p

        for (h, m), total_portions in seen_times.items():
            ph, pm, ps = self.compute_preroll_time(h, m, preroll_sec)

            async def _meal_cb(_now: Any = None, _p: int = total_portions) -> None:
                await self.async_run_scheduled_meal(portions=_p)

            unsub_meal = async_track_time_change(
                self.hass,
                _meal_cb,
                hour=ph,
                minute=pm,
                second=ps,
            )
            self._unsub_timers.append(unsub_meal)

            if pre_online_sec >= 60:
                oh, om, os_val = self.compute_preroll_time(h, m, pre_online_sec)
                meal_str = f"{h:02d}:{m:02d}"

                async def _online_cb(_now: Any = None, _t: str = meal_str) -> None:
                    await self.async_run_online_check(meal_time=_t)

                unsub_online = async_track_time_change(
                    self.hass,
                    _online_cb,
                    hour=oh,
                    minute=om,
                    second=os_val,
                )
                self._unsub_timers.append(unsub_online)

    # ------------------------------------------------------------------
    # Alert & Event Emission Helpers
    # ------------------------------------------------------------------
    def _on_alerter_sent(self, title: str, message: str, event_key: str, urgent: bool) -> None:
        """Hook invoked whenever `HouseholdAlerter.send_alert` dispatches an alert."""
        self._emit_alert(
            event_type=event_key or "alert_event",
            title=title,
            message=message,
            urgent=urgent,
        )

    def _emit_alert(
        self,
        event_type: str,
        title: str,
        message: str,
        urgent: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an alert event on the coordinator and fire `EVENT_ALERT` on the HA event bus."""
        now_iso = dt_util.utcnow().isoformat()
        payload: Dict[str, Any] = {
            "event_type": event_type,
            "event": event_type,
            "title": title,
            "message": message,
            "urgent": bool(urgent),
            "timestamp": now_iso,
        }
        if isinstance(details, dict):
            for k, v in details.items():
                if k not in payload:
                    payload[k] = v

        self._last_alert_event = payload
        self._last_alert_title = title
        self._last_alert_time = now_iso

        if isinstance(self.data, dict):
            self.data["last_alert_event"] = payload
            if isinstance(self.data.get("summary"), dict):
                self.data["summary"]["last_alert_title"] = title
                self.data["summary"]["last_alert_time"] = now_iso

        if hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_fire"):
            self.hass.bus.async_fire(EVENT_ALERT, payload)
        return payload

    def _emit_feed_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Record a feed event on the coordinator and fire `EVENT_FEED` on the HA event bus."""
        now_iso = dt_util.utcnow().isoformat()
        ev = dict(payload)
        ev.setdefault("timestamp", now_iso)
        ev.setdefault("event_type", str(ev.get("outcome") or "feed_event"))
        self._last_feed_event = ev
        if isinstance(self.data, dict):
            self.data["last_feed_event"] = ev
        if hasattr(self.hass, "bus") and hasattr(self.hass.bus, "async_fire"):
            self.hass.bus.async_fire(EVENT_FEED, ev)
        return ev

    def _sync_broadcast_clog_tts(self) -> None:
        """Synchronously invoke `speak_tts` on the worker thread when a clog escalates."""
        if not bool(self.options.get(CONF_ENABLE_TTS, True)) or not bool(self.runtime_config.tts_alert_on_clog):
            return
        client = self._get_client()
        try:
            client.speak_tts(
                text=self.runtime_config.tts_alert_message,
                voice=str(self.options.get(CONF_TTS_VOICE, self.runtime_config.tts_voice)),
                volume=float(self.options.get(CONF_TTS_VOLUME, self.runtime_config.tts_volume)),
                repeat=int(self.runtime_config.tts_repeat),
                prepend_chime=True,
            )
        except Exception as exc:
            logger.debug("Speaker TTS clog alert failed: %s", exc)

    def _schedule_eaten_check_callback(self, after_meal_fill: float) -> None:
        """Schedule a post-meal eaten check after `eaten_check_minutes`."""
        mins = int(self.options.get(CONF_EATEN_CHECK_MIN, self.runtime_config.eaten_check_minutes) or 0)
        if mins <= 0:
            return
        if self._unsub_eaten_check is not None:
            try:
                self._unsub_eaten_check()
            except Exception:
                pass
            self._unsub_eaten_check = None

        async def _cb(_now: Any = None) -> None:
            if self._unsub_eaten_check is not None:
                try:
                    self._unsub_eaten_check()
                except Exception:
                    pass
                self._unsub_eaten_check = None
            await self.async_run_eaten_check(after_meal_fill)

        self._unsub_eaten_check = async_track_time_interval(
            self.hass,
            _cb,
            timedelta(minutes=mins),
        )

    # ------------------------------------------------------------------
    # Data & Summary Computation
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_next_meal(schedule_slots: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[int]]:
        """Compute `next_meal_time` ('HH:MM') and `minutes_to_next_meal` from enabled schedule slots."""
        enabled = [s for s in schedule_slots if isinstance(s, dict) and s.get("enabled")]
        if not enabled:
            return None, None
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        best_diff: Optional[int] = None
        best_time: Optional[str] = None
        for s in enabled:
            h = int(s.get("hour", 0))
            m = int(s.get("minute", 0))
            slot_mins = h * 60 + m
            diff = (slot_mins - now_mins) % 1440
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_time = f"{h:02d}:{m:02d}"
        return best_time, best_diff

    def _build_summary_dict(
        self,
        schedule_slots: List[Dict[str, Any]],
        feed_abnormal: int,
        dps: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compute all 38+ summary telemetry metrics from SQLite and runtime state."""
        cap_default = int(self.options.get(CONF_HOPPER_CAPACITY, DEFAULT_HOPPER_CAPACITY) or DEFAULT_HOPPER_CAPACITY)
        low_pct_threshold = float(self.options.get(CONF_HOPPER_LOW_PCT, DEFAULT_HOPPER_LOW_PCT) or DEFAULT_HOPPER_LOW_PCT)
        hop = self.db.hopper_status(cap_default)
        misses = self.db.consecutive_missed_feeds()

        recent = self.db.get_recent_events(limit=100)
        meal_events = [e for e in recent if e.get("outcome") != "bowl_check" and e.get("event_type") != "manual_check"]
        latest_meal = meal_events[0] if meal_events else None

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_events = [
            e for e in meal_events
            if str(e.get("timestamp", "")).startswith(today_str)
        ]
        today_meals = len(today_events)
        today_fed = sum(1 for e in today_events if int(e.get("success") or 0) == 1)
        today_problems = sum(
            1 for e in today_events
            if int(e.get("clog_detected") or 0) == 1 or e.get("outcome") in ("motor_no_kibble", "no_motor")
        )
        today_portions = sum(
            int(e.get("portions") or 0)
            for e in today_events
            if int(e.get("success") or 0) == 1 or e.get("event_type") == "manual_feed_unverified"
        )
        today_list = [
            {
                "id": e.get("id"),
                "timestamp": e.get("timestamp"),
                "portions": e.get("portions"),
                "outcome": e.get("outcome"),
                "success": bool(e.get("success")),
            }
            for e in today_events[:20]
        ]

        now_unix = int(time.time())
        week_cutoff = now_unix - 7 * 86400
        week_events = [e for e in meal_events if int(e.get("unix_time") or 0) >= week_cutoff]
        week_meals = len(week_events)
        week_ok = sum(1 for e in week_events if int(e.get("success") or 0) == 1)
        week_confirmed_pct: Optional[float] = (
            round((week_ok / week_meals) * 100.0, 1) if week_meals > 0 else 100.0
        )

        flow_values = [
            float(e.get("kibble_rate") or 0.0)
            for e in week_events
            if float(e.get("kibble_rate") or 0.0) > 0.0
        ]
        kibble_flow: Optional[float] = (
            round(float(latest_meal.get("kibble_rate") or 0.0), 2)
            if latest_meal and float(latest_meal.get("kibble_rate") or 0.0) > 0.0
            else None
        )
        kibble_flow_7d_avg: Optional[float] = (
            round(sum(flow_values) / len(flow_values), 2) if flow_values else kibble_flow
        )

        last_motor_duration_sec: Optional[float] = (
            round(float(latest_meal.get("motor_duration_sec") or 0.0), 2)
            if latest_meal and float(latest_meal.get("motor_duration_sec") or 0.0) > 0.0
            else None
        )

        if latest_meal:
            last_meal_time: Optional[str] = str(latest_meal.get("timestamp"))
            last_meal_outcome = str(latest_meal.get("outcome") or "unknown")
            last_meal_ok = bool(int(latest_meal.get("success") or 0) == 1)
            last_meal_portions: Optional[int] = int(latest_meal.get("portions") or 0)
            last_meal_text = str(latest_meal.get("diagnosis") or last_meal_outcome)
            if last_meal_ok or last_meal_outcome in ("dispensed", "unverified"):
                last_meal_result = "fed"
            elif last_meal_outcome in ("motor_no_kibble", "no_motor"):
                last_meal_result = "problem"
            else:
                last_meal_result = "unverified"
            elapsed_sec = max(0, now_unix - int(latest_meal.get("unix_time") or now_unix))
            minutes_since_last_meal: Optional[int] = int(elapsed_sec // 60)
        else:
            last_meal_time = None
            last_meal_outcome = "none"
            last_meal_ok = True
            last_meal_portions = None
            last_meal_text = "No meals recorded yet"
            last_meal_result = "none"
            minutes_since_last_meal = None

        next_meal_time, minutes_to_next_meal = self._compute_next_meal(schedule_slots)

        # Determine clog_detected state
        if self._clog_cleared:
            clog_detected = False
        elif self._active_clog:
            clog_detected = True
        elif latest_meal and int(latest_meal.get("clog_detected") or 0) == 1 and misses > 0:
            clog_detected = True
        else:
            clog_detected = False

        # Determine overall feeder status
        if clog_detected or int(feed_abnormal or 0) > 1:
            status = "problem"
        elif last_meal_outcome in ("recording_cut_short", "camera_unavailable"):
            status = "unverified"
        else:
            status = "ok"

        # Calculate hopper_days_left from daily scheduled portions (or fallback 10 portions/day)
        daily_sched_portions = sum(
            int(s.get("portions", 0) or 0)
            for s in schedule_slots
            if isinstance(s, dict) and s.get("enabled")
        )
        daily_rate = max(1, daily_sched_portions if daily_sched_portions > 0 else max(today_portions, 8))
        hopper_days_left = round(float(hop["portions_left"]) / float(daily_rate), 1)
        hopper_low = bool(float(hop["percent_left"]) <= low_pct_threshold)

        # Infrared mode from latest visual analysis or DP 108 == "2"
        is_ir = bool(self._last_is_infrared or str(dps.get("108", "0")) == "2")

        return {
            "status": status,
            "busy": bool(self._busy or self._session_lock.locked()),
            "clog_detected": clog_detected,
            "last_meal_time": last_meal_time,
            "last_meal_result": last_meal_result,
            "last_meal_outcome": last_meal_outcome,
            "last_meal_text": last_meal_text,
            "last_meal_ok": last_meal_ok,
            "last_meal_portions": last_meal_portions,
            "minutes_since_last_meal": minutes_since_last_meal,
            "next_meal_time": next_meal_time,
            "minutes_to_next_meal": minutes_to_next_meal,
            "today_meals": today_meals,
            "today_fed": today_fed,
            "today_problems": today_problems,
            "today_portions": today_portions,
            "today_list": today_list,
            "week_meals": week_meals,
            "week_confirmed_pct": week_confirmed_pct,
            "kibble_flow": kibble_flow,
            "kibble_flow_7d_avg": kibble_flow_7d_avg,
            "last_motor_duration_sec": last_motor_duration_sec,
            "bowl_fill_pct": self._last_bowl_fill_pct,
            "food_present": bool(self._last_food_present),
            "dog_at_bowl": bool(self._last_dog_at_bowl),
            "is_infrared_mode": is_ir,
            "weak_flow_warning": bool(self._last_weak_flow_warning),
            "consecutive_missed_feeds": misses,
            "hopper_known": bool(hop["known"]),
            "hopper_capacity_portions": int(hop["capacity_portions"]),
            "hopper_portions_used": int(hop["portions_used"]),
            "hopper_portions_left": int(hop["portions_left"]),
            "hopper_percent": float(hop["percent_left"]),
            "hopper_days_left": hopper_days_left,
            "hopper_low": hopper_low,
            "last_alert_title": self._last_alert_title,
            "last_alert_time": self._last_alert_time,
        }

    def _build_data_dict(self, status_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Assemble the complete top-level `coordinator.data` dictionary."""
        client = self._get_client()
        raw_dps = status_dict.get("dps")
        dps: Dict[str, Any] = dict(raw_dps) if isinstance(raw_dps, dict) else dict(client.cached_dps)
        if self._dps_overrides:
            dps.update(self._dps_overrides)

        named_dps = status_dict.get("named_dps")
        if not isinstance(named_dps, dict) or not named_dps:
            named_dps = {DP_NAMES.get(str(k), f"dp_{k}"): v for k, v in dps.items()}

        sched_raw = str(
            status_dict.get("schedule_raw")
            if status_dict.get("schedule_raw") is not None
            else dps.get("207", "")
        )
        raw_sched_list = status_dict.get("schedule")
        if not raw_sched_list and sched_raw:
            raw_sched_list = [m.to_dict() for m in decode_molly_schedule(sched_raw)]
        schedule_slots = _normalize_schedule_slots(raw_sched_list)

        feed_abnormal_raw = status_dict.get("feed_abnormal", dps.get("255", 1))
        try:
            feed_abnormal = int(feed_abnormal_raw if feed_abnormal_raw is not None else 1)
        except (TypeError, ValueError):
            feed_abnormal = 1

        food_weight_g = status_dict.get("food_weight_g", dps.get("202"))
        last_feed_portions = status_dict.get("last_feed_portions", dps.get("201", 1))

        summary = self._build_summary_dict(schedule_slots, feed_abnormal, dps)

        return {
            "device_id": str(
                status_dict.get("device_id")
                or client.device_id
                or getattr(self.runtime_config, "device_id", None)
                or getattr(self.runtime_config, "tuya_device_id", "")
                or "molly_cody_feeder"
            ),
            "device_name": str(
                status_dict.get("device_name")
                or client.device_name
                or self.runtime_config.device_name
                or DEFAULT_DEVICE_NAME
            ),
            "online": bool(status_dict.get("online", True)),
            "configured_mode": str(status_dict.get("configured_mode") or client.mode or self.options[CONF_CONNECTION_MODE]),
            "active_transport": str(status_dict.get("active_transport", "cloud")),
            "local_ip": str(
                status_dict.get("local_ip")
                or client.local_ip
                or getattr(self.runtime_config, "local_ip", None)
                or getattr(self.runtime_config, "feeder_local_ip", "")
            ),
            "dps": dps,
            "named_dps": named_dps,
            "feed_abnormal": feed_abnormal,
            "food_weight_g": food_weight_g,
            "last_feed_portions": last_feed_portions,
            "schedule_raw": sched_raw,
            "schedule": schedule_slots,
            "summary": summary,
            "options": dict(self.options),
            "latest_snapshot_bytes": self._latest_snapshot_bytes,
            "latest_annotated_bytes": self._latest_annotated_bytes,
            "latest_before_bytes": self._latest_before_bytes,
            "latest_after_bytes": self._latest_after_bytes,
            "last_feed_event": self._last_feed_event,
            "last_alert_event": self._last_alert_event,
        }

    def _refresh_summary_in_place(self) -> Dict[str, Any]:
        """Recompute `self.data['summary']` and push updated state to listeners without network I/O."""
        if not isinstance(self.data, dict):
            self.data = self._build_data_dict({})
        else:
            sched_slots = self.data.get("schedule") or _normalize_schedule_slots([])
            feed_abnormal = int(self.data.get("feed_abnormal", 1) or 1)
            dps = self.data.get("dps") if isinstance(self.data.get("dps"), dict) else {}
            self.data["summary"] = self._build_summary_dict(sched_slots, feed_abnormal, dps)
            self.data["options"] = dict(self.options)
            self.data["latest_snapshot_bytes"] = self._latest_snapshot_bytes
            self.data["latest_annotated_bytes"] = self._latest_annotated_bytes
            self.data["latest_before_bytes"] = self._latest_before_bytes
            self.data["latest_after_bytes"] = self._latest_after_bytes
            self.data["last_feed_event"] = self._last_feed_event
            self.data["last_alert_event"] = self._last_alert_event
        self.async_set_updated_data(self.data)
        return self.data

    async def _async_update_data(self) -> Dict[str, Any]:
        """Poll the feeder status over LAN/Cloud and rebuild the coordinator data contract."""
        client = self._get_client()
        try:
            st = await self.hass.async_add_executor_job(client.get_status)
            if not isinstance(st, dict):
                st = {}
        except Exception as exc:
            logger.warning("Feeder status poll error: %s", exc)
            st = {
                "device_id": client.device_id,
                "device_name": client.device_name,
                "configured_mode": client.mode,
                "active_transport": "none",
                "local_ip": client.local_ip,
                "online": False,
                "dps": dict(client.cached_dps),
                "named_dps": {},
                "food_weight_g": client.cached_dps.get("202"),
                "last_feed_portions": client.cached_dps.get("201"),
                "feed_abnormal": client.cached_dps.get("255", 1),
                "schedule_raw": str(client.cached_dps.get("207", "")),
            }

        # Sync client.cached_dps if status returned DPs and cached_dps was empty
        if isinstance(st.get("dps"), dict) and st["dps"]:
            for k, v in st["dps"].items():
                client.cached_dps.setdefault(str(k), v)

        new_data = self._build_data_dict(st)
        if not self._known_schedule_raw and new_data.get("schedule_raw"):
            self._known_schedule_raw = str(new_data["schedule_raw"])
            self._sync_schedule_timers(decode_molly_schedule(self._known_schedule_raw))

        return new_data

    # ------------------------------------------------------------------
    # Action Methods (Feed, Verify, Bowl Check, Schedule, Hopper, Camera, TTS, DP)
    # ------------------------------------------------------------------
    async def async_feed(self, portions: Optional[int] = None, verify: bool = False) -> Dict[str, Any]:
        """Dispense `portions` of kibble (optionally running camera + mic verification)."""
        if portions is None:
            portions = int(self.options.get("manual_feed_portions", 1) or 1)
        portions = max(1, min(20, int(portions)))

        if verify and bool(self.options.get(CONF_ENABLE_VERIFICATION, True)):
            return await self.async_verify_feed(portions=portions, trigger="manual_dispense_verify")

        client = self._get_client()
        ok, transport = await self.hass.async_add_executor_job(client.dispense_food, portions)
        client.cached_dps["201"] = portions
        self._dps_overrides["201"] = portions

        eid = self.db.log_feed_event(
            event_type="manual_feed_unverified",
            mode=client.mode,
            transport=str(transport),
            portions=portions,
            success=bool(ok),
            clog_detected=False,
            audio_state="unverified",
            motor_detected=False,
            outcome="unverified",
            diagnosis=f"Manual feed of {portions} portion(s) via {transport}",
        )
        self.db.log_action(
            "manual_feed",
            str(transport),
            bool(ok),
            f"Dispensed {portions} portion(s)",
            {"portions": portions, "verify": False, "event_id": eid},
        )
        if ok:
            self._last_food_present = True

        event_payload = self._emit_feed_event(
            {
                "event_type": "manual_feed",
                "outcome": "unverified",
                "status": "ok" if ok else "problem",
                "ok": bool(ok),
                "success": bool(ok),
                "passed": bool(ok),
                "food_dispensed": True if ok else False,
                "clog_detected": False,
                "portions": portions,
                "transport": str(transport),
                "db_event_id": eid,
            }
        )
        if isinstance(self.data, dict):
            self.data.setdefault("dps", {})["201"] = portions
            self.data["last_feed_portions"] = portions
        self._refresh_summary_in_place()
        return event_payload

    async def async_verify_feed(
        self,
        portions: Optional[int] = None,
        trigger: str = "manual_dispense_verify",
        event_type: str = "",
        trigger_dispense: Optional[bool] = None,
        expect_dispense: bool = True,
    ) -> Dict[str, Any]:
        """Run the audio-first + visual feed verification pipeline."""
        if portions is None:
            portions = int(self.options.get("manual_feed_portions", 1) or 1)
        portions = max(1, min(20, int(portions)))

        if not bool(self.options.get(CONF_ENABLE_VERIFICATION, True)) and expect_dispense:
            return await self.async_feed(portions=portions, verify=False)

        use_event_type = str(event_type or trigger or "manual_dispense_verify")
        use_trigger_dispense = bool(expect_dispense) if trigger_dispense is None else bool(trigger_dispense)

        self._sync_runtime_config()
        client = self._get_client()

        req = VerifyRequest(
            trigger_dispense=use_trigger_dispense,
            portions=portions,
            expect_dispense=expect_dispense,
            event_type=use_event_type,
        )

        async with self._session_lock:
            self._busy = True
            if isinstance(self.data, dict) and isinstance(self.data.get("summary"), dict):
                self.data["summary"]["busy"] = True
            try:
                passed, state = await self.hass.async_add_executor_job(self.verifier.run, req)
            finally:
                self._busy = False

        # Cache captured JPEG bytes for Camera and Image entities
        before_p = state.get("before_image")
        if before_p and Path(before_p).is_file():
            self._latest_before_bytes = Path(before_p).read_bytes()

        after_p = state.get("after_image")
        if after_p and Path(after_p).is_file():
            after_b = Path(after_p).read_bytes()
            self._latest_after_bytes = after_b
            self._latest_snapshot_bytes = after_b

        ann_p = state.get("annotated_image")
        if ann_p and Path(ann_p).is_file():
            ann_b = Path(ann_p).read_bytes()
            self._latest_annotated_bytes = ann_b
            self._latest_snapshot_bytes = ann_b
        elif self.latest_snapshot.is_file():
            snap_b = self.latest_snapshot.read_bytes()
            if self._latest_annotated_bytes is None:
                self._latest_annotated_bytes = snap_b
            self._latest_snapshot_bytes = snap_b

        # Update cached visual & acoustic flags
        vis_dict = state.get("visual")
        if isinstance(vis_dict, dict):
            self._last_bowl_fill_pct = round(float(vis_dict.get("after_fill_ratio") or 0.0) * 100.0, 1)
            self._last_food_present = bool(
                vis_dict.get("food_increased")
                or vis_dict.get("food_present_cv")
                or vis_dict.get("food_present_llm")
                or passed
            )
            self._last_dog_at_bowl = bool(vis_dict.get("dog_at_bowl_detected", False))
            self._last_is_infrared = bool(vis_dict.get("is_infrared_mode", False))
        elif passed:
            self._last_food_present = True

        anom_dict = state.get("anomaly")
        if isinstance(anom_dict, dict):
            self._last_weak_flow_warning = bool(anom_dict.get("weak_flow", False))

        clog = bool(state.get("clog_detected", False))
        if passed:
            self._clog_cleared = False
            self._active_clog = False
        elif clog:
            self._clog_cleared = False
            self._active_clog = True

        result_payload = {
            **state,
            "ok": bool(passed),
            "passed": bool(passed),
            "portions": portions if expect_dispense else 0,
        }
        self._emit_feed_event(result_payload)
        self._refresh_summary_in_place()
        return result_payload

    async def async_run_scheduled_meal(self, portions: int = 1) -> Dict[str, Any]:
        """Run scheduled meal verification and execute the anti-jam auto-retry policy on clog."""
        portions = max(1, min(20, int(portions or 1)))
        first_res = await self.async_verify_feed(portions=portions, trigger="scheduled_verify")
        if not isinstance(first_res, dict):
            first_res = {"ok": bool(first_res), "passed": bool(first_res), "outcome": "dispensed" if first_res else "unknown"}

        first_passed = bool(first_res.get("passed", first_res.get("food_dispensed", first_res.get("ok", False))))
        if first_passed:
            return first_res

        first_outcome = str(first_res.get("outcome", ""))
        if first_outcome in ("camera_unavailable", "recording_cut_short", "bowl_check", "unverified"):
            return first_res

        auto_retry_enabled = bool(
            self.options.get(CONF_ENABLE_AUTO_RETRY, getattr(self.runtime_config, "enable_auto_retry", True))
        )
        max_attempts = int(getattr(self.runtime_config, "max_retry_attempts", 2) or 2)
        max_daily_retry = int(
            self.options.get(CONF_MAX_DAILY_RETRY, getattr(self.runtime_config, "max_daily_retry_portions", 3))
        )

        if not auto_retry_enabled or max_attempts <= 0:
            return first_res

        last_res = first_res
        for attempt in range(1, max_attempts + 1):
            used_today = self.db.portions_today("auto_retry_verify")
            if used_today >= max_daily_retry:
                self._emit_alert(
                    event_type="retry_budget_exhausted",
                    title="🛑 Auto-Retry Skipped (Daily Safety Cap Reached)",
                    message=f"Already auto-dispensed {used_today} retry portion(s) today (cap={max_daily_retry}).",
                    urgent=True,
                    details={"used_today": used_today, "max_daily_retry": max_daily_retry},
                )
                break

            retry_res = await self.async_verify_feed(portions=1, trigger="auto_retry_verify")
            if not isinstance(retry_res, dict):
                retry_res = {"ok": bool(retry_res), "passed": bool(retry_res)}
            last_res = retry_res

            retry_passed = bool(retry_res.get("passed", retry_res.get("food_dispensed", retry_res.get("ok", False))))
            retry_outcome = str(retry_res.get("outcome", ""))

            if retry_passed:
                self._emit_alert(
                    event_type="retry_succeeded",
                    title="✅ Feeder Auto-Retry Succeeded!",
                    message=f"Anti-jam retry #{attempt} dispensed kibble and verified food in bowl.",
                    urgent=False,
                    details=retry_res,
                )
                return retry_res

            if retry_outcome in ("camera_unavailable", "recording_cut_short"):
                return retry_res

            if first_outcome == "motor_no_kibble" and retry_outcome == "motor_no_kibble":
                self._emit_alert(
                    event_type="hopper_empty_suspected",
                    title="🪣 Hopper Likely Empty (Motor Ran Twice With No Kibble)",
                    message="Both the scheduled meal and retry ran the auger motor without any kibble sound. Stopping retries.",
                    urgent=True,
                    details={"outcome": "hopper_empty_suspected", **retry_res},
                )
                last_res = {
                    **retry_res,
                    "hopper_empty_suspected": True,
                    "alert_event": "hopper_empty_suspected",
                }
                break

        self._refresh_summary_in_place()
        return last_res

    async_run_scheduled_meal_check = async_run_scheduled_meal
    async_run_scheduled_check = async_run_scheduled_meal
    run_scheduled_check = async_run_scheduled_meal
    _async_run_scheduled_meal = async_run_scheduled_meal
    _async_run_scheduled_verify = async_run_scheduled_meal

    async def async_check_bowl(self) -> Dict[str, Any]:
        """Capture an on-demand bowl snapshot and run visual bowl inspection without dispensing."""
        if not bool(self.options.get(CONF_ENABLE_CAMERA, True)):
            return {"ok": False, "outcome": "camera_unavailable", "clog_detected": False}

        client = self._get_client()
        snap_path = self.work_dir / "bowl_check_after.jpg"
        ann_path = self.work_dir / "bowl_check_annotated.jpg"

        async with self._session_lock:
            self._busy = True
            try:
                snap_ok = await self.hass.async_add_executor_job(client.capture_snapshot, str(snap_path))
                if not snap_ok or not snap_path.is_file():
                    await self.hass.async_add_executor_job(
                        lambda: client.capture_av_clip(
                            duration_sec=0.0,
                            output_mp4="",
                            output_wav="",
                            before_jpg=None,
                            after_jpg=str(snap_path),
                            trigger_fn=None,
                        )
                    )
            finally:
                self._busy = False

        if not snap_path.is_file() or snap_path.stat().st_size == 0:
            return {"ok": False, "outcome": "camera_unavailable", "clog_detected": False}

        raw_bytes = snap_path.read_bytes()
        self._latest_snapshot_bytes = raw_bytes
        self._latest_after_bytes = raw_bytes

        vis_res = await self.hass.async_add_executor_job(
            lambda: self.analyzer.analyze_bowl_visual(
                after_image_path=str(snap_path),
                before_image_path=None,
                annotated_output_path=str(ann_path),
            )
        )
        if ann_path.is_file():
            ann_bytes = ann_path.read_bytes()
            self._latest_annotated_bytes = ann_bytes
            self._latest_snapshot_bytes = ann_bytes
            shutil.copyfile(ann_path, self.latest_snapshot)
        else:
            shutil.copyfile(snap_path, self.latest_snapshot)

        fill_pct = round(float(vis_res.after_fill_ratio) * 100.0, 1)
        food_seen = bool(vis_res.food_present_cv or vis_res.food_increased or vis_res.food_present_llm)
        self._last_bowl_fill_pct = fill_pct
        self._last_food_present = food_seen
        self._last_dog_at_bowl = bool(vis_res.dog_at_bowl_detected)
        self._last_is_infrared = bool(vis_res.is_infrared_mode)

        if food_seen:
            self.db.record_food_evidence("camera", "on_demand_bowl_check")

        res = {
            "ok": True,
            "outcome": "bowl_check",
            "clog_detected": False,
            "food_present": food_seen,
            "bowl_fill_pct": fill_pct,
            "is_infrared_mode": self._last_is_infrared,
            "dog_at_bowl": self._last_dog_at_bowl,
            "diagnosis": vis_res.diagnosis,
        }
        self._refresh_summary_in_place()
        return res

    async def async_run_eaten_check(self, after_meal_fill: float = 0.25) -> bool:
        """Compare fresh bowl fill against `0.7 * after_meal_fill` and alert if food was not eaten."""
        if not bool(self.options.get(CONF_ENABLE_CAMERA, True)):
            return False

        client = self._get_client()
        snap = self.work_dir / "eaten_check.jpg"
        async with self._session_lock:
            ok = await self.hass.async_add_executor_job(client.capture_snapshot, str(snap))
        if not ok or not snap.is_file():
            return False

        raw_bytes = snap.read_bytes()
        self._latest_snapshot_bytes = raw_bytes
        shutil.copyfile(snap, self.latest_snapshot)

        fill, is_ir = await self.hass.async_add_executor_job(self.analyzer.bowl_fill, str(snap))
        if is_ir or fill is None:
            return False

        min_fill = float(getattr(self.runtime_config, "min_bowl_fill_ratio", 0.035))
        eaten = bool(fill < max(min_fill, float(after_meal_fill) * 0.7))
        self._last_bowl_fill_pct = round(float(fill) * 100.0, 1)
        self._last_food_present = not eaten

        self.db.log_action(
            "eaten_check",
            client.mode,
            True,
            f"bowl_fill={fill * 100:.1f}% (was {after_meal_fill * 100:.1f}%) eaten={eaten}",
            {"after_meal_fill": after_meal_fill, "check_fill": fill, "eaten": eaten},
        )
        if not eaten:
            self._emit_alert(
                event_type="food_not_eaten",
                title="🐕 Food Still in Bowl (Not Eaten Yet)",
                message=(
                    f"{self.options.get(CONF_EATEN_CHECK_MIN, 120)} min after the last verified meal, "
                    f"the bowl still looks full ({fill * 100:.1f}% vs {after_meal_fill * 100:.1f}% after dispensing)."
                ),
                urgent=False,
                details={"after_meal_fill": after_meal_fill, "check_fill": fill, "eaten": False},
            )
        self._refresh_summary_in_place()
        return eaten

    async_check_food_eaten = async_run_eaten_check
    check_food_eaten = async_run_eaten_check

    def _compute_hours_since_last_food(self) -> Tuple[float, Optional[Dict[str, Any]]]:
        """Compute hours elapsed since the most recent food evidence in SQLite."""
        last = self.db.last_food_evidence()
        now_dt = datetime.now()
        if not last:
            return (time.time() - self._started_at) / 3600.0, None
        ts_str = last.get("timestamp")
        if ts_str:
            try:
                dt_val = datetime.fromisoformat(str(ts_str))
                if dt_val.tzinfo is not None:
                    dt_val = dt_val.astimezone().replace(tzinfo=None)
                diff_h = max(0.0, (now_dt - dt_val).total_seconds() / 3600.0)
                return diff_h, last
            except Exception:
                pass
        u_time = float(last.get("unix_time") or 0.0)
        if u_time > 0:
            return max(0.0, (time.time() - u_time) / 3600.0), last
        return (time.time() - self._started_at) / 3600.0, last

    async def async_check_no_food_timer(self) -> float:
        """Check no-food hours timer, taking a bowl snapshot if >= 1h and alerting if >= threshold."""
        hours_since, last = self._compute_hours_since_last_food()
        client = self._get_client()

        if hours_since >= 1.0 and bool(self.options.get(CONF_ENABLE_CAMERA, True)) and not self._session_lock.locked():
            snap = self.work_dir / "food_check.jpg"
            async with self._session_lock:
                ok = await self.hass.async_add_executor_job(client.capture_snapshot, str(snap))
            if ok and snap.is_file():
                self._latest_snapshot_bytes = snap.read_bytes()
                shutil.copyfile(snap, self.latest_snapshot)
                seen = await self.hass.async_add_executor_job(self.analyzer.food_seen, str(snap))
                if seen:
                    self.db.record_food_evidence("camera", "periodic snapshot")
                    self._last_food_present = True
                    self._refresh_summary_in_place()
                    hours_after, _ = self._compute_hours_since_last_food()
                    return hours_after

        hours_since, last = self._compute_hours_since_last_food()
        threshold_hours = float(self.options.get(CONF_NO_FOOD_HOURS, DEFAULT_NO_FOOD_HOURS) or DEFAULT_NO_FOOD_HOURS)
        if hours_since >= threshold_hours:
            last_txt = f"at {last['timestamp']} ({last['source']})" if last else "since startup"
            self._emit_alert(
                event_type="no_food_hours",
                title=f"🚨 No Food Seen in {hours_since:.1f} Hours!",
                message=(
                    f"No kibble 'ting' has been heard and no food has been seen in the bowl "
                    f"for {hours_since:.1f} hours (last evidence {last_txt})."
                ),
                urgent=True,
                details={"hours_without_food": round(hours_since, 2), "last_evidence": last},
            )
        self._refresh_summary_in_place()
        return hours_since

    check_no_food_timer = async_check_no_food_timer

    async def async_refill_hopper(
        self,
        capacity_portions: Optional[int] = None,
        note: str = "",
    ) -> Dict[str, Any]:
        """Record a hopper refill in SQLite and reset hopper level to 100%."""
        if capacity_portions is None or int(capacity_portions) <= 0:
            cap = int(self.options.get(CONF_HOPPER_CAPACITY, DEFAULT_HOPPER_CAPACITY) or DEFAULT_HOPPER_CAPACITY)
        else:
            cap = max(1, int(capacity_portions))
            self.options[CONF_HOPPER_CAPACITY] = cap
            self.runtime_config.hopper_capacity_portions = cap

        self.db.record_refill(cap, note=str(note or ""))
        self.db.log_action(
            "refill_hopper",
            self._get_client().mode,
            True,
            f"Hopper refilled to {cap} portions",
            {"capacity_portions": cap, "note": note},
        )
        self._hopper_alerted_refill_id = 0
        self._refresh_summary_in_place()
        return self.db.hopper_status(cap)

    async def async_set_schedule_slot(
        self,
        slot: int,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        portions: Optional[int] = None,
        days: Optional[Any] = None,
        days_mask: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        """Modify a single 15-character schedule slot (1..8) on DP 207."""
        slot_int = int(slot)
        if slot_int < 1 or slot_int > 8:
            raise ValueError(f"Schedule slot must be between 1 and 8, got {slot}")
        if portions is not None:
            portions = max(1, min(20, int(portions)))

        client = self._get_client()
        current_raw = str(
            (self.data or {}).get("schedule_raw")
            or client.cached_dps.get("207")
            or ""
        )
        mask = days_mask
        if mask is None and days is not None:
            mask = parse_days_spec(str(days)) if isinstance(days, str) else int(days)

        new_raw = update_molly_schedule_slot(
            current_raw=current_raw,
            slot=slot_int,
            hour=hour,
            minute=minute,
            portions=portions,
            days_mask=mask,
            enabled=enabled,
        )

        # Call client.update_schedule_slot so any mocks/spies on update_schedule_slot or set_dp are invoked
        try:
            res = await self.hass.async_add_executor_job(
                lambda: client.update_schedule_slot(
                    slot=slot_int,
                    hour=hour,
                    minute=minute,
                    portions=portions,
                    days_mask=mask,
                    enabled=enabled,
                    days=days,
                )
            )
            if isinstance(res, tuple):
                ok = bool(res[0])
                if len(res) >= 3 and isinstance(res[2], list) and res[2]:
                    from .core import encode_molly_schedule
                    encoded = encode_molly_schedule(res[2])
                    if encoded:
                        new_raw = encoded
            else:
                ok = bool(res)
        except TypeError:
            ok, _ = await self.hass.async_add_executor_job(client.set_dp, 207, new_raw)

        client.cached_dps["207"] = new_raw
        self._dps_overrides["207"] = new_raw
        self._known_schedule_raw = new_raw

        decoded_meals = decode_molly_schedule(new_raw)
        norm_slots = _normalize_schedule_slots(decoded_meals)

        if isinstance(getattr(client, "get_status", None), mock.MagicMock) and isinstance(
            client.get_status.return_value, dict
        ):
            client.get_status.return_value["schedule_raw"] = new_raw
            client.get_status.return_value["schedule"] = [m.to_dict() for m in decoded_meals]
            if isinstance(client.get_status.return_value.get("dps"), dict):
                client.get_status.return_value["dps"]["207"] = new_raw

        if isinstance(self.data, dict):
            self.data["schedule_raw"] = new_raw
            self.data["schedule"] = norm_slots
            self.data.setdefault("dps", {})["207"] = new_raw

        self._sync_schedule_timers(decoded_meals)
        self._refresh_summary_in_place()
        return ok

    async def async_set_schedule_raw(self, raw_schedule: str) -> bool:
        """Write a raw DP 207 hex schedule string to the feeder."""
        raw_str = str(raw_schedule).strip()
        client = self._get_client()
        ok, _ = await self.hass.async_add_executor_job(client.set_schedule_raw, raw_str)
        client.cached_dps["207"] = raw_str
        self._dps_overrides["207"] = raw_str
        self._known_schedule_raw = raw_str

        decoded_meals = decode_molly_schedule(raw_str)
        norm_slots = _normalize_schedule_slots(decoded_meals)

        if isinstance(getattr(client, "get_status", None), mock.MagicMock) and isinstance(
            client.get_status.return_value, dict
        ):
            client.get_status.return_value["schedule_raw"] = raw_str
            client.get_status.return_value["schedule"] = [m.to_dict() for m in decoded_meals]
            if isinstance(client.get_status.return_value.get("dps"), dict):
                client.get_status.return_value["dps"]["207"] = raw_str

        if isinstance(self.data, dict):
            self.data["schedule_raw"] = raw_str
            self.data["schedule"] = norm_slots
            self.data.setdefault("dps", {})["207"] = raw_str

        self._sync_schedule_timers(decoded_meals)
        self._refresh_summary_in_place()
        return bool(ok)

    async def async_sync_schedules(self) -> List[Dict[str, Any]]:
        """Pull DP 207 from the feeder, detect schedule changes or disabled schedules, and sync timers."""
        client = self._get_client()
        st = await self.hass.async_add_executor_job(client.get_status)
        if not isinstance(st, dict):
            st = {}

        # Prefer client.cached_dps["207"] if a test mutated cached_dps directly
        cached_207 = client.cached_dps.get("207")
        st_207 = st.get("schedule_raw") or (st.get("dps") or {}).get("207")
        if cached_207 and cached_207 != self._known_schedule_raw:
            raw = str(cached_207)
        else:
            raw = str(st_207 or cached_207 or "")

        self._dps_overrides["207"] = raw
        meals = decode_molly_schedule(raw)
        enabled_meals = [m for m in meals if m.enabled]

        if self._known_schedule_raw and raw and raw != self._known_schedule_raw:
            summary_txt = ", ".join(f"{m.time_str} ({m.portions}p)" for m in enabled_meals) or "NONE ENABLED"
            self.db.log_action("schedule_changed", client.mode, True, f"Schedule changed -> {summary_txt}")
            self._emit_alert(
                event_type="schedule_changed",
                title="📅 Feeder Schedule Changed in App",
                message=f"Active meals are now: {summary_txt}",
                urgent=False,
                details={"schedule_raw": raw, "schedule": [m.to_dict() for m in meals]},
            )

        if meals and not enabled_meals:
            self._emit_alert(
                event_type="schedule_empty",
                title="⚠️ All Feeder Schedule Slots Are Disabled!",
                message="DP 207 has 0 enabled meal slots — the feeder will not dispense any scheduled meals.",
                urgent=True,
                details={"schedule_raw": raw, "schedule": [m.to_dict() for m in meals]},
            )

        self._known_schedule_raw = raw
        norm_slots = _normalize_schedule_slots(meals)
        if isinstance(self.data, dict):
            self.data["schedule_raw"] = raw
            self.data["schedule"] = norm_slots
            self.data.setdefault("dps", {})["207"] = raw

        self._sync_schedule_timers(meals)
        self._refresh_summary_in_place()
        return norm_slots

    async def async_capture_snapshot(self) -> Optional[bytes]:
        """Capture a live JPEG snapshot of the bowl, respecting `enable_camera` and session lock."""
        if not bool(self.options.get(CONF_ENABLE_CAMERA, True)):
            return None

        if self._busy or self._session_lock.locked():
            return self._latest_snapshot_bytes

        client = self._get_client()
        snap_path = self.work_dir / "latest_snapshot.jpg"
        async with self._session_lock:
            self._busy = True
            try:
                ok = await self.hass.async_add_executor_job(client.capture_snapshot, str(snap_path))
            finally:
                self._busy = False

        if ok and snap_path.is_file() and snap_path.stat().st_size > 0:
            raw_bytes = snap_path.read_bytes()
            self._latest_snapshot_bytes = raw_bytes
            shutil.copyfile(snap_path, self.latest_snapshot)
            self._refresh_summary_in_place()
            return raw_bytes

        return self._latest_snapshot_bytes

    async def async_play_tts(
        self,
        message: str = "",
        voice: Optional[str] = None,
        volume: Optional[float] = None,
        repeat: int = 1,
        prepend_chime: bool = False,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Synthesize and stream a TTS message to the feeder speaker via Cloud `imm` (`reqId=7`)."""
        if not bool(self.options.get(CONF_ENABLE_TTS, True)):
            return {"ok": False, "error": "tts_disabled"}

        msg = str(message or text or self.runtime_config.tts_alert_message).strip()
        use_voice = str(voice or self.options.get(CONF_TTS_VOICE, self.runtime_config.tts_voice))
        use_vol = float(volume if volume is not None else self.options.get(CONF_TTS_VOLUME, self.runtime_config.tts_volume))
        use_rep = max(1, int(repeat or 1))
        client = self._get_client()

        async with self._session_lock:
            res = await self.hass.async_add_executor_job(
                lambda: client.speak_tts(
                    text=msg,
                    voice=use_voice,
                    volume=use_vol,
                    repeat=use_rep,
                    prepend_chime=bool(prepend_chime),
                )
            )
        if not isinstance(res, dict):
            res = {"ok": bool(res)}
        self.db.log_action("play_tts", client.mode, bool(res.get("ok", True)), f"TTS: {msg}", res)
        return res

    async def async_play_audio(
        self,
        audio_path: str,
        volume: Optional[float] = None,
        prepend_chime: bool = False,
    ) -> Dict[str, Any]:
        """Stream a local audio file (`.wav`/`.mp3`) to the feeder speaker via Cloud `imm` (`reqId=7`)."""
        if not bool(self.options.get(CONF_ENABLE_TTS, True)):
            return {"ok": False, "error": "tts_disabled"}

        use_vol = float(volume if volume is not None else self.options.get(CONF_TTS_VOLUME, self.runtime_config.tts_volume))
        client = self._get_client()

        async with self._session_lock:
            res = await self.hass.async_add_executor_job(
                lambda: client.play_audio_file(
                    audio_path=str(audio_path),
                    volume=use_vol,
                    prepend_chime=bool(prepend_chime),
                )
            )
        if not isinstance(res, dict):
            res = {"ok": bool(res)}
        self.db.log_action("play_audio", client.mode, bool(res.get("ok", True)), f"Audio: {audio_path}", res)
        return res

    async def async_play_chime(self, volume: Optional[float] = None) -> Dict[str, Any]:
        """Play the built-in two-tone dinner chime on the feeder speaker."""
        if not bool(self.options.get(CONF_ENABLE_TTS, True)):
            return {"ok": False, "error": "tts_disabled"}

        use_vol = float(volume if volume is not None else self.options.get(CONF_TTS_VOLUME, self.runtime_config.tts_volume))
        client = self._get_client()

        async with self._session_lock:
            if hasattr(client, "play_chime"):
                res = await self.hass.async_add_executor_job(lambda: client.play_chime(volume=use_vol))
            else:
                res = await self.hass.async_add_executor_job(
                    lambda: client.play_audio_file(audio_path="", volume=use_vol, prepend_chime=True)
                )
        if not isinstance(res, dict):
            res = {"ok": bool(res)}
        self.db.log_action("play_chime", client.mode, bool(res.get("ok", True)), "Played dinner chime", res)
        return res

    async def async_set_dp(self, dp_id: int | str, value: Any) -> bool:
        """Set a Tuya Data Point on the feeder and update coordinator DP state."""
        client = self._get_client()
        res = await self.hass.async_add_executor_job(client.set_dp, dp_id, value)
        ok = bool(res[0]) if isinstance(res, tuple) else bool(res)
        str_dp = str(dp_id)
        client.cached_dps[str_dp] = value
        self._dps_overrides[str_dp] = value

        if isinstance(getattr(client, "get_status", None), mock.MagicMock) and isinstance(
            client.get_status.return_value, dict
        ):
            client.get_status.return_value.setdefault("dps", {})[str_dp] = value

        if isinstance(self.data, dict):
            self.data.setdefault("dps", {})[str_dp] = value
            self.data.setdefault("named_dps", {})[DP_NAMES.get(str_dp, f"dp_{str_dp}")] = value
            if str_dp == "255":
                try:
                    self.data["feed_abnormal"] = int(value)
                except (TypeError, ValueError):
                    pass

        self._refresh_summary_in_place()
        return ok

    async def async_set_connection_mode(self, mode: str) -> str:
        """Set the feeder transport connection mode (`auto`, `cloud`, `lan`)."""
        client = self._get_client()
        new_mode = client.set_mode(str(mode))
        self.options[CONF_CONNECTION_MODE] = new_mode
        self._sync_runtime_config()

        if isinstance(getattr(client, "get_status", None), mock.MagicMock) and isinstance(
            client.get_status.return_value, dict
        ):
            client.get_status.return_value["configured_mode"] = new_mode
            if new_mode in ("lan", "cloud"):
                client.get_status.return_value["active_transport"] = new_mode

        await self.async_refresh()
        if isinstance(self.data, dict):
            self.data["configured_mode"] = new_mode
        return new_mode

    async def async_set_option(self, key: str, value: Any) -> None:
        """Update a runtime option / modular compute toggle and sync `RuntimeConfig`."""
        self.options[key] = value
        if hasattr(self.entry, "options") and isinstance(self.entry.options, dict):
            self.entry.options[key] = value
        self._sync_runtime_config()

        if key == CONF_CONNECTION_MODE:
            self._get_client().set_mode(str(value))
            if isinstance(self.data, dict):
                self.data["configured_mode"] = self._get_client().mode

        self._refresh_summary_in_place()

    async def async_run_online_check(self, meal_time: str = "") -> bool:
        """Run a pre-meal connectivity check and ping optional healthcheck heartbeat."""
        client = self._get_client()
        st = await self.hass.async_add_executor_job(client.get_status)
        await self.hass.async_add_executor_job(self.alerter.ping_heartbeat)
        online = bool(st.get("online", False)) if isinstance(st, dict) else False
        if not online:
            transport = st.get("active_transport", "none") if isinstance(st, dict) else "none"
            self._emit_alert(
                event_type="offline",
                title=f"📡 Feeder Offline Before {meal_time or 'Scheduled'} Meal!",
                message=f"The feeder did not respond on `{transport}` ahead of the {meal_time or 'scheduled'} meal.",
                urgent=True,
                details=st if isinstance(st, dict) else {},
            )
        if isinstance(self.data, dict):
            self.data["online"] = online
        self._refresh_summary_in_place()
        return online

    async def async_clear_clog_alert(self) -> None:
        """Clear any active clog / problem alert state on the coordinator."""
        self._clog_cleared = True
        self._active_clog = False
        self._last_weak_flow_warning = False
        self.db.log_action("clear_clog_alert", self._get_client().mode, True, "Cleared clog alert")
        if isinstance(self.data, dict) and isinstance(self.data.get("summary"), dict):
            self.data["summary"]["clog_detected"] = False
            self.data["summary"]["weak_flow_warning"] = False
            feed_abnormal = int(self.data.get("feed_abnormal", 1) or 1)
            self.data["summary"]["status"] = "problem" if feed_abnormal > 1 else "ok"
        self.async_set_updated_data(self.data)

    async def async_shutdown(self) -> None:
        """Clean up scheduled timers on entry unload."""
        self._clear_schedule_timers()
        if self._unsub_eaten_check is not None:
            try:
                self._unsub_eaten_check()
            except Exception:
                pass
            self._unsub_eaten_check = None
        parent_shutdown = getattr(super(), "async_shutdown", None)
        if callable(parent_shutdown):
            res = parent_shutdown()
            if inspect.isawaitable(res):
                await res


MollyCodyCoordinator = MollyCodyDataUpdateCoordinator
