"""Housekeeping & health jobs: hopper level, eaten check, daily summary, capture retention, config checks."""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("poor_puppy.maintenance")


class MaintenanceMixin:
    """Periodic health / housekeeping jobs run by the daemon."""

    def check_food_eaten(self, after_meal_fill: float) -> Optional[bool]:
        """Health check: EATEN_CHECK_MINUTES after a verified meal the bowl should be (mostly) empty.
        Daylight only; returns None when it can't tell."""
        snap = str(self.work_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_eaten_check.jpg")
        try:
            if not self.molly.capture_snapshot(snap):
                return None
        except Exception as exc:
            logger.warning("Eaten-check snapshot failed: %s", exc)
            return None
        fill_now, is_ir = self.analyzer.bowl_fill(snap)
        if fill_now is None:
            return None
        eaten = fill_now < 0.7 * after_meal_fill
        logger.info("Eaten check: fill %.1f%% -> %.1f%% (eaten=%s)", after_meal_fill * 100, fill_now * 100, eaten)
        if not eaten:
            self.alerter.send_alert(
                title="Puppy may not be eating",
                message=(f"{self.cfg.eaten_check_minutes} min after the meal the bowl still looks full "
                         f"({after_meal_fill * 100:.0f}% → {fill_now * 100:.0f}% fill)."),
                image_path=snap,
                event="food_not_eaten",
                urgent=False,
            )
        return eaten

    def _check_hopper_level(self) -> None:
        hop = self.db.hopper_status(self.cfg.hopper_capacity_portions)
        self.latest_state["hopper"] = hop
        if hop["known"] and hop["percent_left"] is not None and hop["percent_left"] <= self.cfg.hopper_low_percent:
            self.alerter.send_alert(
                title=f"Kibble hopper low: ~{hop['percent_left']:.0f}% left",
                message=(f"About {hop['portions_left']} of {hop['capacity_portions']} portions left since the last "
                         "refill. Refill soon, then run `./kibble refill` (or POST /webhook/refill)."),
                event="hopper_low",
                urgent=False,
                cooldown_minutes=12 * 60,
            )

    def check_no_food_timer(self) -> Optional[float]:
        """Timer mode: look at the bowl; alert ONLY if there has been no evidence of food (verified feed
        with the kibble ting, or food seen in a snapshot) for NO_FOOD_ALERT_HOURS. Returns hours since food."""
        snap = str(self.work_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_food_check.jpg")
        try:
            if self.molly.capture_snapshot(snap) and self.analyzer.food_seen(snap):
                self.db.record_food_evidence("camera", "periodic snapshot")
        except Exception as exc:
            logger.warning("Food-check snapshot failed: %s", exc)
        last = self.db.last_food_evidence()
        if last is None:
            return None  # fresh install: nothing to compare against yet
        hours = (datetime.now() - datetime.fromisoformat(last["timestamp"])).total_seconds() / 3600.0
        logger.info("No-food timer: last food evidence %.1f h ago (%s)", hours, last["source"])
        if hours >= self.cfg.no_food_alert_hours:
            self.alerter.send_alert(
                title=f"NO FOOD SEEN FOR {hours:.0f} HOURS",
                message=(f"No feed with the kibble 'ting' and no food visible in the bowl since {last['timestamp']} "
                         f"(limit {self.cfg.no_food_alert_hours:g} h). Check the feeder / hopper."),
                image_path=snap if Path(snap).exists() else None,
                event="no_food_timer",
                cooldown_minutes=self.cfg.no_food_alert_hours * 60,
            )
        return hours

    def send_daily_summary(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        events = self.db.get_events_on(today)
        ok = sum(1 for e in events if e.get("success"))
        bad = [e for e in events if not e.get("success")]
        portions = sum(int(e.get("portions") or 0) for e in events if e.get("success"))
        hop = self.db.hopper_status(self.cfg.hopper_capacity_portions)
        lines = [f"• Feeds verified OK: {ok}/{len(events)} (≈{portions} portions)"]
        for e in bad[:5]:
            lines.append(f"• ⚠️ {str(e.get('timestamp'))[11:16]} {e.get('outcome') or e.get('audio_state')}")
        if hop["known"]:
            lines.append(f"• Hopper: ~{hop['percent_left']:.0f}% left ({hop['portions_left']} portions)")
        self.alerter.send_alert(
            title="Daily feeder summary" + (" — all good" if events and not bad else ""),
            message="\n".join(lines),
            event="daily_summary",
            urgent=bool(bad),
            cooldown_minutes=0,
        )

    def prune_captures(self) -> Dict[str, int]:
        """Retention pass (startup + hourly): see watchdog/service/retention.py for the policy."""
        return self.retention.run()

    def validate_config(self) -> List[str]:
        """Startup sanity checks for silent misconfigurations that would leave the pet unprotected."""
        problems: List[str] = []
        if not (self.molly.device_id or self.cfg.tuya_device_id):
            problems.append("TUYA_DEVICE_ID is not set")
        if not self.molly.session_file.exists() and not self.cfg.molly_user:
            problems.append(f"No session file ({self.molly.session_file}) and no MOLLY_USER/MOLLY_PASS to log in")
        if not any(
            (self.cfg.discord_webhook_url, self.cfg.telegram_bot_token, self.cfg.ntfy_topic_url, self.cfg.ha_webhook_url)
        ):
            problems.append("No alert channel configured (DISCORD/TELEGRAM/NTFY/HA): clog alerts will only be logged")
        if Path("/.dockerenv").exists() and not os.getenv("TZ"):
            problems.append("TZ is not set inside Docker: the container runs on UTC, meal checks will fire at the wrong time")
        for p in problems:
            logger.warning("CONFIG: %s", p)
        logger.info("Local time is %s (%s)", datetime.now().strftime("%Y-%m-%d %H:%M"), time.tzname[0])
        return problems
