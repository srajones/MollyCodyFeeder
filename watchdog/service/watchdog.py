"""PuppyFeederWatchdog: wires the feeder client, analyzer, DB and alerts together and runs the daemon."""
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any, Dict, Tuple

import schedule

from watchdog.alerter import HouseholdAlerter
from watchdog.analysis import BowlAnalyzer
from watchdog.config import WatchdogConfig
from watchdog.db import KibbleDatabase
from watchdog.molly import MollyFeederClient
from watchdog.service.http_api import HttpApiMixin
from watchdog.service.maintenance import MaintenanceMixin
from watchdog.service.models import VerifyRequest
from watchdog.service.retention import RetentionManager
from watchdog.service.scheduling import SchedulingMixin
from watchdog.service.verifier import FeedVerifier

logger = logging.getLogger("poor_puppy.main")


class PuppyFeederWatchdog(SchedulingMixin, MaintenanceMixin, HttpApiMixin):
    """
    Lightweight VPS/LAN watchdog for the Molly & Cody Smart Feeder 6L Camera:
    - `FeedVerifier` (verifier.py)     verifies each meal (audio + vision + telemetry)
    - `SchedulingMixin` (scheduling.py) watches every scheduled meal, retries / escalates
    - `MaintenanceMixin` (maintenance.py) hopper, eaten check, daily summary, retention, config checks
    - `HttpApiMixin` (http_api.py)      local token-protected HTTP API
    """

    def __init__(self, config: WatchdogConfig):
        self.cfg = config
        self.db = KibbleDatabase(config.db_path)
        self.analyzer = BowlAnalyzer(config)
        self.alerter = HouseholdAlerter(config)
        # Alert history for the control page: every alert that actually goes out is logged
        self.alerter.on_sent = lambda title, message, event, urgent: self.db.log_action(
            action="alert", transport="notify", success=True, summary=title,
            payload={"event": event, "urgent": bool(urgent), "message": str(message)[:2000]},
        )
        self.molly = MollyFeederClient(
            email=config.molly_user,
            password=config.molly_pass,
            mode=config.feeder_mode,
            device_id=config.tuya_device_id,
            local_ip=config.feeder_local_ip,
            local_key=config.feeder_local_key,
        )
        self.work_dir = Path(os.getenv("CAPTURES_DIR", "/tmp/poor_puppy_captures"))
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.latest_snapshot_path = self.work_dir / "latest_annotated.jpg"
        self._last_fresh_snapshot = 0.0
        self._busy = threading.Lock()
        self.latest_state: Dict[str, Any] = {
            "status": "idle",
            "mode": self.molly.mode,
            "last_check": None,
            "clog_detected": False,
            "food_dispensed": None,
        }
        self.retention = RetentionManager(self.cfg, self.db, self.work_dir, protected=[self.latest_snapshot_path])
        self.verifier = FeedVerifier(
            cfg=self.cfg,
            molly=self.molly,
            analyzer=self.analyzer,
            db=self.db,
            alerter=self.alerter,
            work_dir=self.work_dir,
            latest_snapshot_path=self.latest_snapshot_path,
            on_meal_verified=self.check_food_eaten,
            after_event=self._check_hopper_level,
            retention=self.retention,
        )

    def trigger_retry_feed(self, portions: int = 1) -> Tuple[bool, str]:
        return self.molly.dispense_food(portions)

    def verify_feed_event(self, **kwargs) -> bool:
        """Runs one verification at a time (schedule, webhooks and CLI share one camera).
        Keyword arguments are the fields of `VerifyRequest`."""
        with self._busy:
            passed, state = self.verifier.run(VerifyRequest(**kwargs))
            hopper = self.latest_state.get("hopper")
            self.latest_state = state
            if hopper is not None and "hopper" not in state:
                state["hopper"] = hopper
            return passed

    def start_daemon(self) -> None:
        self.validate_config()
        self.start_webhook_server()
        meal_times = self.sync_schedule_jobs()

        # Self-healing periodic schedule & cloud session sync every 30 minutes
        schedule.every(30).minutes.do(self.sync_schedule_jobs).tag("self_heal")
        self.prune_captures()  # once at startup, then hourly
        schedule.every().hour.do(self.prune_captures).tag("retention")
        if self.cfg.heartbeat_url:
            self.alerter.ping_heartbeat()
            schedule.every(5).minutes.do(self.alerter.ping_heartbeat).tag("heartbeat")
        if self.cfg.alert_mode == "no_food_hours":
            schedule.every(max(5, self.cfg.food_check_interval_min)).minutes.do(self.check_no_food_timer).tag("food_timer")
            logger.info("Alert mode: only when no food is seen for %g h (bowl checked every %d min)",
                        self.cfg.no_food_alert_hours, self.cfg.food_check_interval_min)
        if self.cfg.daily_summary_time:
            schedule.every().day.at(self.cfg.daily_summary_time).do(self.send_daily_summary).tag("summary")

        logger.info("Puppy Feeder Watchdog running (mode=%s). Monitoring meals: %s", self.molly.mode.upper(), meal_times)
        stop = threading.Event()

        def _graceful(signum, _frame):
            logger.info("Received signal %s: finishing the current check and shutting down", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _graceful)
        signal.signal(signal.SIGINT, _graceful)
        while not stop.is_set():
            schedule.run_pending()
            stop.wait(1)
