"""Schedule sync, pre-meal online check, and scheduled meal verification with anti-jam retry (`SchedulingMixin`)."""
from __future__ import annotations

import logging
import time
from typing import Dict, List

from custom_components.molly_cody_feeder.core.env import _install_schedule_fallback

_install_schedule_fallback()
import schedule  # noqa: E402

from custom_components.molly_cody_feeder.core.schedule_decoder import MealEntry

logger = logging.getLogger("molly_cody_feeder.scheduling")


class SchedulingMixin:
    """Syncs DP 207 schedule slots and runs pre-meal / meal-time verification."""

    @staticmethod
    def _offset_hhmm(t_str: str, offset_sec: int) -> str:
        parts = [int(x) for x in t_str.strip().split(":")]
        h, m = parts[0], parts[1]
        s = parts[2] if len(parts) > 2 else 0
        total = (h * 3600 + m * 60 + s + int(offset_sec)) % 86400
        rh, rem = divmod(total, 3600)
        rm, rs = divmod(rem, 60)
        return f"{rh:02d}:{rm:02d}:{rs:02d}"

    def sync_schedules_from_device(self) -> List[MealEntry]:
        entries: List[MealEntry] = []
        raw = ""
        try:
            st = self.molly.get_status()
            raw = str(st.get("schedule_raw", ""))
            entries = self.molly.get_schedule()
            self._check_schedule_change(raw, entries)
            enabled = [e for e in entries if e.enabled]
            if enabled:
                schedule.clear("meal")
                preroll = max(0, min(300, int(self.cfg.meal_preroll_seconds)))
                pre_sec = max(0, int(self.cfg.pre_meal_online_check_seconds))
                seen: Dict[str, int] = {}
                for e in enabled:
                    seen[e.time_str] = seen.get(e.time_str, 0) + e.portions
                for t_str, portions in seen.items():
                    fire_at = self._offset_hhmm(t_str, -preroll)
                    rec_Hint = max(self.cfg.record_duration_seconds, int(portions * 2.6 + 8)) + preroll
                    logger.info(
                        "Scheduled meal check at %s (meal %s, -%ds preroll, %d portion(s), ~%ds window)",
                        fire_at, t_str, preroll, portions, rec_Hint,
                    )
                    schedule.every().day.at(fire_at).do(self._run_scheduled_meal_check, portions=portions).tag("meal")
                    if pre_sec >= 60:
                        pre_at = self._offset_hhmm(t_str, -pre_sec)
                        schedule.every().day.at(pre_at).do(
                            self._pre_meal_online_check, meal_time=t_str
                        ).tag("meal")
                self.last_state["device_schedule"] = [e.to_dict() for e in entries]
                return entries
        except Exception as exc:
            logger.warning("Could not fetch schedule from feeder (%s), using .env fallback", exc)

        schedule.clear("meal")
        for t_str in self.cfg.feed_schedule_times:
            fire_at = self._offset_hhmm(t_str, -max(0, int(self.cfg.meal_preroll_seconds)))
            logger.info("Scheduled fallback meal check at %s (meal %s)", fire_at, t_str)
            schedule.every().day.at(fire_at).do(self._run_scheduled_meal_check, portions=1).tag("meal")
        return entries

    def _check_schedule_change(self, raw: str, entries: List[MealEntry]) -> None:
        if not raw:
            return
        enabled = [e for e in entries if e.enabled]
        if self._known_schedule_raw and raw != self._known_schedule_raw:
            summary = ", ".join(f"{e.time_str} ({e.portions}p)" for e in enabled) or "NONE ENABLED"
            self.db.log_action("schedule_changed", self.molly.mode, True, f"Schedule changed -> {summary}")
            self.alerter.send_alert(
                title="📅 Feeder Schedule Changed in App",
                message=f"Active meals are now: **{summary}**",
                details={"schedule": [e.to_dict() for e in entries]},
                event="schedule_changed", urgent=False, cooldown_minutes=1,
            )
        if entries and not enabled:
            self.alerter.send_alert(
                title="⚠️ All Feeder Schedule Slots Are Disabled!",
                message="DP 207 has 0 enabled meal slots — the feeder will not dispense any scheduled meals.",
                details={"schedule": [e.to_dict() for e in entries]},
                event="schedule_empty", urgent=True, cooldown_minutes=180,
            )
        self._known_schedule_raw = raw

    def _pre_meal_online_check(self, meal_time: str = "") -> bool:
        try:
            st = self.molly.get_status()
            self.alerter.ping_heartbeat()
            if not st.get("online"):
                self.alerter.send_alert(
                    title=f"📡 Feeder Offline Before {meal_time or 'Scheduled'} Meal!",
                    message=(
                        f"The feeder did not respond on `{st.get('active_transport')}` "
                        f"ahead of the {meal_time} meal. Check power and Wi-Fi."
                    ),
                    details=st, event="feeder_offline", urgent=True,
                )
                return False
            return True
        except Exception as exc:
            logger.warning("Pre-meal online check failed: %s", exc)
            return False

    def _run_scheduled_meal_check(self, portions: int = 1) -> None:
        preroll = max(0, min(300, int(self.cfg.meal_preroll_seconds)))
        old_dur = self.cfg.record_duration_seconds
        try:
            self.cfg.record_duration_seconds = max(old_dur, int(portions * 2.6 + 8)) + preroll
            passed = self.run_verification(trigger_dispense=False, portions=portions, expect_dispense=True)
        finally:
            self.cfg.record_duration_seconds = old_dur
        if passed:
            return
        last_outcome = str(self.last_state.get("outcome", ""))
        if last_outcome in ("camera_unavailable", "recording_cut_short"):
            logger.warning("Skipping auto-retry: meal outcome was %s (cannot confirm meal failed)", last_outcome)
            return
        if self.cfg.auto_retry_on_clog and self.cfg.max_retry_attempts > 0:
            self._auto_retry_after_failed_meal()
            return
        if self.last_state.get("clog_detected"):
            self.verifier._broadcast_clog_tts()

    def _auto_retry_after_failed_meal(self) -> None:
        first_outcome = self.last_state.get("outcome")
        for attempt in range(1, self.cfg.max_retry_attempts + 1):
            used_today = self.db.portions_today("auto_retry_verify")
            if used_today >= self.cfg.max_daily_retry_portions:
                logger.warning(
                    "Daily auto-retry cap reached (%d/%d portions); skipping retry",
                    used_today, self.cfg.max_daily_retry_portions,
                )
                self.alerter.send_alert(
                    title="🛑 Auto-Retry Skipped (Daily Safety Cap Reached)",
                    message=f"Already auto-dispensed {used_today} retry portion(s) today.",
                    details=self.last_state, event="retry_budget_exhausted", urgent=True,
                )
                break
            logger.warning("Attempting anti-jam retry %d/%d...", attempt, self.cfg.max_retry_attempts)
            time.sleep(5)
            if self.run_verification(
                trigger_dispense=True, portions=1, expect_dispense=True, event_type="auto_retry_verify"
            ):
                self.alerter.send_alert(
                    title="✅ Feeder Auto-Retry Succeeded!",
                    message=f"Anti-jam retry #{attempt} dispensed kibble and verified food in bowl.",
                    image_path=str(self.latest_snapshot) if self.latest_snapshot.exists() else None,
                    details=self.last_state, event="retry_succeeded", urgent=False, cooldown_minutes=0,
                )
                return
            retry_outcome = self.last_state.get("outcome")
            if retry_outcome in ("camera_unavailable", "recording_cut_short"):
                logger.warning("Stopping auto-retry: retry #%d outcome was %s", attempt, retry_outcome)
                return
            if first_outcome == "motor_no_kibble" and retry_outcome == "motor_no_kibble":
                logger.warning("Motor ran twice with 0 kibble tings -> hopper likely empty, stopping retries")
                self.alerter.send_alert(
                    title="🪣 Hopper Likely Empty (Motor Ran Twice With No Kibble)",
                    message="Both the scheduled meal and retry ran the auger motor without any kibble sound. Stopping retries.",
                    image_path=str(self.latest_snapshot) if self.latest_snapshot.exists() else None,
                    details=self.last_state, event="hopper_empty_suspected", urgent=True, cooldown_minutes=0,
                )
                break
        if self.last_state.get("clog_detected"):
            self.verifier._broadcast_clog_tts()
