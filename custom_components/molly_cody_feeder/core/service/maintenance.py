"""Periodic maintenance checks (`MaintenanceMixin`): eaten check, hopper level, daily rollup, no-food timer."""
from __future__ import annotations

import logging
import shutil
import time
from typing import Any, Dict

from custom_components.molly_cody_feeder.core.env import _install_schedule_fallback

_install_schedule_fallback()
import schedule  # noqa: E402

logger = logging.getLogger("molly_cody_feeder.maintenance")


class MaintenanceMixin:
    """Eaten check, hopper level check, no-food timer, and daily rollup."""

    def _schedule_eaten_check(self, after_meal_fill: float) -> None:
        mins = max(1, int(self.cfg.eaten_check_minutes))
        schedule.clear("eaten-check")

        def _once() -> Any:
            self._run_eaten_check(after_meal_fill)
            return schedule.CancelJob

        schedule.every(mins).minutes.do(_once).tag("eaten-check")

    def _run_eaten_check(self, after_meal_fill: float) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            snap = self.work_dir / "eaten_check.jpg"
            if not self.molly.capture_snapshot(str(snap)):
                return False
            fill, is_ir = self.analyzer.bowl_fill(str(snap))
            shutil.copyfile(snap, self.latest_snapshot)
            if is_ir or fill is None:
                return False
            eaten = fill < max(self.cfg.min_bowl_fill_ratio, after_meal_fill * 0.7)
            self.db.log_action(
                "eaten_check", self.molly.mode, True,
                f"bowl_fill={fill * 100:.1f}% (was {after_meal_fill * 100:.1f}%) eaten={eaten}",
                {"after_meal_fill": after_meal_fill, "check_fill": fill, "eaten": eaten},
            )
            if not eaten:
                self.alerter.send_alert(
                    title="🐕 Food Still in Bowl (Not Eaten Yet)",
                    message=(
                        f"{self.cfg.eaten_check_minutes} min after the last verified meal, "
                        f"the bowl still looks full ({fill * 100:.1f}% vs {after_meal_fill * 100:.1f}% after dispensing)."
                    ),
                    image_path=str(snap),
                    details={"after_meal_fill": after_meal_fill, "check_fill": fill},
                    event="food_not_eaten", urgent=False,
                )
            return eaten
        finally:
            self._lock.release()

    def _check_hopper_level(self) -> Dict[str, Any]:
        hop = self.db.hopper_status(self.cfg.hopper_capacity_portions)
        self.last_state["hopper"] = hop
        if hop["known"] and hop["percent_left"] <= self.cfg.hopper_low_percent:
            refill_id = hop["refill_id"] or 0
            if self._hopper_alerted_refill_id != refill_id:
                self._hopper_alerted_refill_id = refill_id
                self.alerter.send_alert(
                    title=f"🪣 Feeder Hopper Low (~{hop['percent_left']:.0f}% / {hop['portions_left']} portions left)",
                    message=(
                        f"{hop['portions_used']} of {hop['capacity_portions']} portions used since refill "
                        f"on {hop['refilled_at']}. Refill soon and call `molly_cody_feeder.refill_hopper`."
                    ),
                    details=hop, event="hopper_low", urgent=False, cooldown_minutes=360,
                )
        return hop

    def _check_no_food_timer(self) -> None:
        if self.cfg.alert_mode != "no_food_hours":
            return
        now = time.time()
        last = self.db.last_food_evidence()
        hours_since = (now - last["unix_time"]) / 3600.0 if last else (now - self._started_at) / 3600.0

        if hours_since >= 1.0 and self._lock.acquire(blocking=False):
            try:
                snap = self.work_dir / "food_check.jpg"
                if self.molly.capture_snapshot(str(snap)):
                    shutil.copyfile(snap, self.latest_snapshot)
                    if self.analyzer.food_seen(str(snap)):
                        self.db.record_food_evidence("camera", "periodic snapshot")
                        logger.info("No-food timer reset: food visible in bowl snapshot")
                        return
            finally:
                self._lock.release()

        last = self.db.last_food_evidence()
        hours_since = (now - last["unix_time"]) / 3600.0 if last else (now - self._started_at) / 3600.0
        logger.info(
            "No-food timer: last food evidence %.1f h ago (%s)",
            hours_since, last["source"] if last else "none since startup",
        )
        if hours_since < self.cfg.no_food_alert_hours:
            return
        last_txt = f"at {last['timestamp']} ({last['source']})" if last else "since the watchdog started"
        snap = self.latest_snapshot if self.latest_snapshot.exists() else None
        self.alerter.send_alert(
            title=f"🚨 No Food Seen in {hours_since:.1f} Hours!",
            message=(
                f"No kibble 'ting' has been heard and no food has been clearly seen in the bowl "
                f"for **{hours_since:.1f} hours** (last evidence {last_txt}). Please check the feeder."
            ),
            image_path=str(snap) if snap else None,
            details={"hours_without_food": round(hours_since, 2), "last_evidence": last},
            event="no_food_timer", urgent=True, cooldown_minutes=120,
        )

    def _send_daily_summary(self) -> Dict[str, Any]:
        roll = self.db.daily_rollup()
        hop = self.db.hopper_status(self.cfg.hopper_capacity_portions)
        hop_txt = (
            f"{hop['portions_left']}/{hop['capacity_portions']} portions (~{hop['percent_left']:.0f}%)"
            if hop["known"]
            else f"{hop['portions_used']} portions used (call `molly_cody_feeder.refill_hopper` after filling)"
        )
        msg = (
            f"• **Meals verified**: {roll['meals_verified']} ({roll['portions_dispensed']} portions)\n"
            f"• **Failed / clogs**: {roll['meals_failed']} (auto-retries: {roll['retries']})\n"
            f"• **Hopper estimate**: {hop_txt}"
        )
        self.alerter.send_alert(
            title=f"🐾 Daily Feeder Summary ({roll['date']})",
            message=msg,
            image_path=str(self.latest_snapshot) if self.latest_snapshot.exists() else None,
            details={"rollup": roll, "hopper": hop},
            event="daily_summary", urgent=False, cooldown_minutes=60,
        )
        return roll
