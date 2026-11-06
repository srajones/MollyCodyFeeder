import html
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
import requests

from watchdog.config import WatchdogConfig

logger = logging.getLogger("poor_puppy.alerter")

# HA payload honesty: only these events mean "the dog may not have been fed" (clog / no food).
# Every other alert is informational (camera unreachable, recording cut short, schedule changed, ...)
# and must not be reported to Home Assistant as a clog.
CLOG_EVENTS = frozenset({
    "feed_clog_detected",  # N feeds in a row without the kibble sound
    "feed_no_ting_first",  # first feed without the kibble sound (heads-up)
    "hopper_empty_suspected",  # motor ran on the meal AND the retry, no kibble either time
    "retry_budget_exhausted",  # meal failed and the daily auto-retry budget is used up
    "no_food_timer",  # ALERT_MODE=no_food_hours: no evidence of food for N hours
})
# Events where food is known to have reached the bowl
FED_EVENTS = frozenset({"feed_verified", "retry_succeeded", "dispense_warning"})
# Events gated by HA_NOTIFY_ON_SUCCESS (routine "all good" pushes); every other alert always reaches HA
SUCCESS_EVENTS = frozenset({"feed_verified"})

_UNSET: Any = object()


def plain_text(text: Any) -> str:
    """Strips markdown bold markers (Home Assistant notifications show `**` literally)."""
    return str(text if text is not None else "").replace("**", "")


def event_flags(event: str) -> "tuple[bool, Optional[bool]]":
    """(clog_detected, food_dispensed) for an alert event. food_dispensed None = unknown."""
    if event in CLOG_EVENTS:
        return True, False
    if event in FED_EVENTS:
        return False, True
    return False, None


class HouseholdAlerter:
    """
    Dispatches urgent clog/empty-bowl alerts and structured telemetry to:
    - Home Assistant Webhooks (`POST /api/webhook/<webhook_id>`)
    - Discord Webhooks (with annotated snapshot attachment)
    - Telegram Bot (with annotated snapshot photo)
    - ntfy.sh Push Notifications
    """

    def __init__(self, config: WatchdogConfig):
        self.cfg = config
        self._last_sent: Dict[str, float] = {}
        self._urgent = True
        # Optional hook (set by the daemon) so every alert that goes out is recorded for the alert history page
        self.on_sent = None

    def _redact(self, text: Any) -> str:
        s = str(text)
        for secret in (
            self.cfg.telegram_bot_token,
            self.cfg.ha_webhook_url,
            self.cfg.discord_webhook_url,
            self.cfg.ntfy_topic_url,
            self.cfg.heartbeat_url,
        ):
            if secret and len(secret) > 4:
                s = s.replace(secret, "<REDACTED>")
                try:
                    path = urlsplit(secret).path
                    if path and len(path) > 1:
                        s = s.replace(path, "/<REDACTED>")
                except Exception:
                    pass
        s = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<REDACTED>", s)
        s = re.sub(r"/api/webhooks?/[^/\s'\"]+(?:/[^/\s'\"]+)?", "/api/webhooks/<REDACTED>", s)
        s = re.sub(
            r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            "/<REDACTED-UUID>",
            s,
        )
        return s

    def send_ha_webhook(
        self,
        event: str,
        clog_detected: bool,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        force: bool = False,
        food_dispensed: Optional[bool] = _UNSET,
        verified: Optional[bool] = None,
        outcome: Optional[str] = None,
    ) -> bool:
        """
        Sends a structured JSON webhook payload to Home Assistant (`POST /api/webhook/<id>`).
        Returns True if the HTTP POST succeeded, False otherwise.

        Payload flags: `clog_detected` (a real clog / no-food event), `food_dispensed` (True / False, or None =
        unknown, e.g. camera unreachable or informational alerts), `verified` (True when `food_dispensed` is a
        real verdict rather than unknown) and `outcome` (verifier outcome when available). Text is plain
        (markdown `**` stripped).
        """
        if not self.cfg.ha_webhook_url:
            return False
        if not force and not clog_detected and not self.cfg.ha_notify_on_success:
            return False

        if food_dispensed is _UNSET:
            food_dispensed = False if clog_detected else event_flags(event)[1]
        if verified is None:
            verified = food_dispensed is not None
        if outcome is None and details:
            outcome = details.get("outcome")
        snapshot_url = f"{self.cfg.public_base_url}/snapshot.jpg" if self.cfg.public_base_url else "/snapshot.jpg"
        flags: Dict[str, Any] = {
            "clog_detected": bool(clog_detected),
            "food_dispensed": food_dispensed,
            "verified": bool(verified),
        }
        payload: Dict[str, Any] = {
            "event": event,
            "event_type": event,
            **flags,
            "outcome": outcome,
            "title": plain_text(title),
            "message": plain_text(message),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "snapshot_url": snapshot_url,
        }
        if details:
            payload.update(details)
            payload.update(flags)  # verdict flags always reflect this event, never a stale copy in details

        try:
            resp = requests.post(self.cfg.ha_webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            if not (200 <= int(getattr(resp, "status_code", 200)) < 300):
                raise RuntimeError(f"HTTP {resp.status_code}")
            logger.info("Sent Home Assistant webhook (%s) -> HTTP %d", event, resp.status_code)
            return True
        except Exception as exc:
            logger.error("Home Assistant webhook failed: %s", self._redact(exc))
            return False

    def send_ha_tts(self, text: str, title: str = "Poor Puppy TTS") -> bool:
        """
        Sends a dedicated TTS event (`event_type="tts_speak"`) to Home Assistant via HA_WEBHOOK_URL.
        Bypasses HA_NOTIFY_ON_SUCCESS gating and does not set food_dispensed/clog_detected flags.
        """
        if not self.cfg.ha_webhook_url:
            return False
        payload: Dict[str, Any] = {
            "event": "tts_speak",
            "event_type": "tts_speak",
            "title": title,
            "message": text,
            "tts_text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            resp = requests.post(self.cfg.ha_webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            if not (200 <= int(getattr(resp, "status_code", 200)) < 300):
                raise RuntimeError(f"HTTP {resp.status_code}")
            logger.info("Sent Home Assistant TTS webhook -> HTTP %d", resp.status_code)
            return True
        except Exception as exc:
            logger.error("Home Assistant TTS webhook failed: %s", self._redact(exc))
            return False

    def send_alert(
        self,
        title: str,
        message: str,
        image_path: Optional[str] = None,
        event: str = "feed_clog_detected",
        details: Optional[Dict[str, Any]] = None,
        urgent: bool = True,
        cooldown_minutes: Optional[float] = None,
    ) -> bool:
        """Returns False if suppressed by the per-event cooldown (no notification spam)."""
        cd = self.cfg.alert_cooldown_minutes if cooldown_minutes is None else cooldown_minutes
        now = time.monotonic()
        last = self._last_sent.get(event)
        if cd > 0 and last is not None and (now - last) < cd * 60:
            logger.info("Alert '%s' suppressed (cooldown %.0f min): %s", event, cd, title)
            return False
        self._last_sent[event] = now
        self._urgent = urgent
        logger.warning("🚨 HOUSEHOLD ALERT: %s — %s", title, message)

        # 1. Home Assistant Webhook (only real clog / no-food events are flagged as clogs; every alert
        # reaches HA — HA_NOTIFY_ON_SUCCESS only gates routine "feed verified" pushes)
        clog_detected, food_dispensed = event_flags(event)
        self.send_ha_webhook(
            event=event,
            clog_detected=clog_detected,
            title=title,
            message=message,
            details=details,
            force=event not in SUCCESS_EVENTS,
            food_dispensed=food_dispensed,
        )

        # 2. Discord Webhook
        if self.cfg.discord_webhook_url:
            self._send_discord(title, message, image_path)

        # 3. Telegram Bot
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            self._send_telegram(title, message, image_path)

        # 4. ntfy.sh
        if self.cfg.ntfy_topic_url:
            self._send_ntfy(title, message, image_path)
        if self.on_sent is not None:
            try:
                self.on_sent(title=title, message=message, event=event, urgent=urgent)
            except Exception as exc:  # the history record must never break an alert
                logger.debug("Alert history hook failed: %s", exc)
        return True

    def ping_heartbeat(self) -> None:
        """Dead-man switch: ping HEARTBEAT_URL (e.g. healthchecks.io) so the owner is told if the
        watchdog itself (VPS, container, network) stops running."""
        if not self.cfg.heartbeat_url:
            return
        try:
            requests.get(self.cfg.heartbeat_url, timeout=10)
        except Exception as exc:
            logger.debug("Heartbeat ping failed: %s", self._redact(exc))

    def _send_discord(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            payload = {"content": f"🐶 **{title}**\n{message}"[:2000]}
            if image_path and Path(image_path).exists():
                with open(image_path, "rb") as f:
                    requests.post(
                        self.cfg.discord_webhook_url,
                        data=payload,
                        files={"file": ("bowl_snapshot.jpg", f, "image/jpeg")},
                        timeout=15,
                    ).raise_for_status()
            else:
                requests.post(self.cfg.discord_webhook_url, json=payload, timeout=15).raise_for_status()
        except Exception as exc:
            logger.error("Discord alert failed: %s", self._redact(exc))

    def _send_telegram(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            # HTML parse mode with escaping: legacy Markdown rejected messages containing "_" or "*"
            caption = f"🐶 <b>{html.escape(title)}</b>\n{html.escape(message.replace('**', ''))}"[:1024]
            if image_path and Path(image_path).exists():
                url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendPhoto"
                with open(image_path, "rb") as f:
                    requests.post(
                        url,
                        data={"chat_id": self.cfg.telegram_chat_id, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": f},
                        timeout=15,
                    ).raise_for_status()
            else:
                url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
                requests.post(
                    url,
                    json={"chat_id": self.cfg.telegram_chat_id, "text": caption, "parse_mode": "HTML"},
                    timeout=15,
                ).raise_for_status()
        except Exception as exc:
            logger.error("Telegram alert failed: %s", self._redact(exc))

    def _send_ntfy(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            # HTTP headers must be latin-1: strip emoji/markdown from the title, keep the body as UTF-8
            safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip() or "Puppy feeder alert"
            urgent = getattr(self, "_urgent", True)
            headers = {
                "Title": safe_title,
                "Priority": "urgent" if urgent else "default",
                "Tags": "dog,warning,rotating_light" if urgent else "dog",
            }
            if image_path and Path(image_path).exists():
                # ONE push with the photo attached and the text in the Message header (was 2 pushes)
                headers["Filename"] = "bowl_snapshot.jpg"
                headers["Message"] = message.replace("**", "").replace("\n", " | ").encode(
                    "latin-1", "ignore"
                ).decode("latin-1")[:3500]
                with open(image_path, "rb") as f:
                    requests.put(self.cfg.ntfy_topic_url, data=f, headers=headers, timeout=15).raise_for_status()
            else:
                requests.post(
                    self.cfg.ntfy_topic_url, data=message.encode("utf-8"), headers=headers, timeout=15
                ).raise_for_status()
        except Exception as exc:
            logger.error("ntfy alert failed: %s", self._redact(exc))
