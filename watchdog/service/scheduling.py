"""Meal scheduling: DP 207 sync with pre-roll, scheduled checks + anti-clog retry policy, schedule-change alerts."""

import logging
from typing import Dict, List

import schedule

from watchdog.service.decision import CAMERA_UNAVAILABLE, RECORDING_CUT_SHORT

# Runs that captured nothing reliable: a retry would be a blind dispense (overfeed risk)
_NO_RETRY_OUTCOMES = (CAMERA_UNAVAILABLE, RECORDING_CUT_SHORT)

logger = logging.getLogger("poor_puppy.scheduling")


class SchedulingMixin:
    """Schedules a verification for every enabled meal and runs the retry / escalation policy."""

    def run_scheduled_check(self, meal_portions: int = 0) -> None:
        # Don't announce TTS until auto-retry (if enabled) has also failed.
        # The job fires MEAL_PREROLL_SECONDS early; record across the meal time so the feeder's
        # own dispense is captured even if its clock / MCU start is a bit off (auto-extends while
        # the motor is still running).
        ok = self.verify_feed_event(
            trigger_dispense=False,
            announce_clog_tts=not self.cfg.auto_retry_on_clog,
            record_seconds=float(self.cfg.meal_preroll_seconds + self.cfg.record_duration_seconds + 15),
            expected_portions=meal_portions,
            expect_dispense=True,
        )
        if ok or not self.cfg.auto_retry_on_clog:
            return
        first_outcome = self.latest_state.get("outcome")
        if first_outcome in _NO_RETRY_OUTCOMES:
            return  # unverified (camera unreachable / stream dropped) != clogged: never auto-dispense blind
        for attempt in range(1, self.cfg.max_retry_attempts + 1):
            # Overfeed guard: never exceed the daily budget of automatic retry portions
            used = self.db.portions_today("auto_retry_verify")
            if used + 1 > self.cfg.max_daily_retry_portions:
                logger.warning("Auto-retry skipped: daily retry budget used (%d/%d portions)",
                               used, self.cfg.max_daily_retry_portions)
                self.alerter.send_alert(
                    title="Auto-retry limit reached — please check the feeder",
                    message=(f"Already auto-dispensed {used} extra portion(s) today "
                             f"(limit {self.cfg.max_daily_retry_portions}). No more automatic retries."),
                    image_path=str(self.latest_snapshot_path) if self.latest_snapshot_path.exists() else None,
                    event="retry_budget_exhausted",
                    details=self.latest_state.get("details"),
                )
                return
            logger.warning("Attempting anti-jam retry %d/%d...", attempt, self.cfg.max_retry_attempts)
            is_last_attempt = attempt == self.cfg.max_retry_attempts
            if self.verify_feed_event(
                trigger_dispense=True,
                portions=1,
                announce_clog_tts=is_last_attempt,
                event_type="auto_retry_verify",
            ):
                self.alerter.send_alert(
                    title="Anti-Jam Retry Succeeded!",
                    message=f"Retry #{attempt} successfully dispensed kibble into the bowl.",
                    image_path=str(self.latest_snapshot_path) if self.latest_snapshot_path.exists() else None,
                    event="retry_succeeded",
                    details=self.latest_state.get("details"),
                )
                return
            if self.latest_state.get("outcome") in _NO_RETRY_OUTCOMES:
                return  # the retry itself couldn't be verified: another blind dispense could overfeed
            # Motor spun twice with no kibble: more retries just grind an empty hopper / jam harder
            if first_outcome == "motor_no_kibble" and self.latest_state.get("outcome") == "motor_no_kibble":
                self.alerter.send_alert(
                    title="HOPPER LIKELY EMPTY (or chute jammed) — retries stopped",
                    message=("The motor ran on the scheduled meal AND on the retry, but no kibble reached the "
                             "bowl either time. Please refill the hopper / clear the chute."),
                    image_path=str(self.latest_snapshot_path) if self.latest_snapshot_path.exists() else None,
                    event="hopper_empty_suspected",
                    details=self.latest_state.get("details"),
                )
                return

    def sync_schedule_jobs(self) -> List[str]:
        """Reads DP 207 from the feeder and updates the daily scheduled verification jobs."""
        meal_times: List[str] = list(self.cfg.feed_schedule_times)
        portions_at: Dict[str, int] = {}
        try:
            raw, meals = self.molly.get_schedule()
            enabled_meals = [m for m in meals if m.enabled]
            self._notice_schedule_change(raw, meals)
            if enabled_meals:
                meal_times = sorted({m.time_str for m in enabled_meals})
                for m in enabled_meals:
                    portions_at[m.time_str] = portions_at.get(m.time_str, 0) + int(m.portions)
                logger.info("Synced enabled feeding times from feeder DP 207: %s", meal_times)
        except Exception as exc:
            logger.warning("Could not sync schedule from feeder, using fallback: %s", exc)

        schedule.clear("meal_checks")
        preroll = max(0, int(self.cfg.meal_preroll_seconds))
        pre_check = max(0, int(self.cfg.pre_meal_online_check_seconds))

        def _at(t_str: str, seconds_before: int) -> str:
            hh, mm = (int(x) for x in t_str.split(":")[:2])
            start = (hh * 3600 + mm * 60 - seconds_before) % 86400
            return f"{start // 3600:02d}:{(start % 3600) // 60:02d}:{start % 60:02d}"

        for t_str in meal_times:
            schedule.every().day.at(_at(t_str, preroll)).do(
                self.run_scheduled_check, meal_portions=portions_at.get(t_str, 0)
            ).tag("meal_checks")
            if pre_check:
                schedule.every().day.at(_at(t_str, preroll + pre_check)).do(
                    self.check_feeder_online, meal_time=t_str
                ).tag("meal_checks")
        return meal_times

    def _notice_schedule_change(self, raw: str, meals: list) -> None:
        """Alert when meals get disabled/removed in the app (or all meals are off) — a silently
        disabled schedule starves the pet just as surely as a clog."""
        summary = {m.time_str: (m.portions, m.days_str) for m in meals if m.enabled}
        prev = getattr(self, "_last_schedule", None)
        self._last_schedule = summary
        if not raw:
            return
        if not summary:
            self.alerter.send_alert(
                title="Feeder has NO enabled meals",
                message="All schedule slots are disabled — the feeder will not dispense on its own.",
                event="schedule_empty",
                cooldown_minutes=12 * 60,
            )
            return
        if prev is None or prev == summary:
            return
        gone = sorted(set(prev) - set(summary))
        changed = sorted(t for t in summary if t in prev and prev[t] != summary[t])
        added = sorted(set(summary) - set(prev))
        lines = [f"• removed/disabled {t} ({prev[t][0]}p)" for t in gone]
        lines += [f"• changed {t}: {prev[t][0]}p → {summary[t][0]}p ({summary[t][1]})" for t in changed]
        lines += [f"• added {t} ({summary[t][0]}p, {summary[t][1]})" for t in added]
        self.alerter.send_alert(
            title="Feeding schedule changed",
            message="\n".join(lines),
            event="schedule_changed",
            urgent=bool(gone),
            cooldown_minutes=0,
        )

    def check_feeder_online(self, meal_time: str = "") -> bool:
        """Warn BEFORE a meal if the feeder is offline, while there's still time to fix it."""
        try:
            online = bool(self.molly.get_status().get("online"))
        except Exception as exc:
            logger.warning("Pre-meal online check failed: %s", exc)
            online = False
        if not online:
            self.alerter.send_alert(
                title=f"Feeder appears OFFLINE before the {meal_time} meal",
                message="The cloud reports the feeder offline (power / Wi-Fi?). The upcoming meal may not dispense.",
                event="feeder_offline",
            )
        return online
