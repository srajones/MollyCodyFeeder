"""Household alert dispatcher (`HouseholdAlerter`) for the Molly & Cody Smart Feeder."""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import requests

from custom_components.molly_cody_feeder.core.config import RuntimeConfig

logger = logging.getLogger("molly_cody_feeder.alerter")

CLOG_EVENTS = frozenset({
    "feed_clog_detected",
    "feed_no_ting_first",
    "hopper_empty_suspected",
    "retry_budget_exhausted",
    "no_food_timer",
})
FED_EVENTS = frozenset({"feed_verified", "retry_succeeded", "dispense_warning"})
SUCCESS_EVENTS = frozenset({"feed_verified"})

_UNSET: Any = object()


def plain_text(text: Any) -> str:
    return str(text if text is not None else "").replace("**", "")


def event_flags(event: str) -> "tuple[bool, Optional[bool]]":
    if event in CLOG_EVENTS:
        return True, False
    if event in FED_EVENTS:
        return False, True
    return False, None


class HouseholdAlerter:
    """Dispatches clog/empty-bowl alerts and structured telemetry to HA callbacks and webhooks."""

    def __init__(self, config: Optional[RuntimeConfig] = None):
        self.cfg = config or RuntimeConfig()
        self._last_sent: Dict[str, float] = {}
        self._urgent = True
        self.on_sent: Optional[Callable[[str, str, str, bool], None]] = None

    def ping_heartbeat(self) -> bool:
        if not self.cfg.heartbeat_url:
            return False
        try:
            resp = requests.get(self.cfg.heartbeat_url, timeout=8)
            return resp.status_code < 400
        except Exception as exc:
            logger.debug("Heartbeat ping failed: %s", exc)
            return False

    def send_alert(
        self,
        title: str,
        message: str,
        image_path: Optional[str] = None,
        video_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        event: str = "",
        urgent: Any = _UNSET,
        cooldown_minutes: Optional[float] = None,
    ) -> None:
        key = event or title
        cd_min = self.cfg.alert_cooldown_minutes if cooldown_minutes is None else cooldown_minutes
        now = time.monotonic()
        if cd_min > 0 and key in self._last_sent and (now - self._last_sent[key]) < cd_min * 60:
            logger.info("Alert '%s' suppressed (cooldown %.0f min): %s", key, cd_min, title)
            return
        self._last_sent[key] = now
        self._urgent = (event in CLOG_EVENTS or event == "") if urgent is _UNSET else bool(urgent)
        logger.warning("HOUSEHOLD ALERT: %s — %s", title, message)
        if self.on_sent:
            try:
                self.on_sent(title, message, key, self._urgent)
            except Exception as exc:
                logger.debug("on_sent hook failed: %s", exc)

        if self.cfg.ha_webhook_url and (self.cfg.ha_notify_on_success or key not in SUCCESS_EVENTS):
            self._send_home_assistant(title, message, details, event=key)
        if self._urgent and self.cfg.ha_tts_webhook_url:
            self._send_home_assistant_tts(title, message, details, event=key)
        if self.cfg.discord_webhook_url:
            self._send_discord(title, message, image_path)
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            self._send_telegram(title, message, image_path)
        if self.cfg.ntfy_topic_url:
            self._send_ntfy(title, message, image_path)

    def notify_event(
        self,
        event: str,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.cfg.ha_webhook_url:
            return
        if event in SUCCESS_EVENTS and not self.cfg.ha_notify_on_success:
            return
        self._urgent = False
        self._send_home_assistant(title, message, details, event=event)

    @staticmethod
    def _redact_webhook_url(url: str) -> str:
        try:
            parts = urlsplit(url)
            masked_path = re.sub(r"(/api/webhook/)[^/?#]+", r"\1***", parts.path)
            return f"{parts.scheme}://{parts.netloc}{masked_path}"
        except Exception:
            return "<redacted_webhook_url>"

    def _build_ha_payload(
        self,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        event: str = "",
    ) -> Dict[str, Any]:
        det = dict(details) if isinstance(details, dict) else {}
        clog_flag, fed_flag = event_flags(event or "feed_clog_detected")
        clean_title = plain_text(title)
        clean_msg = plain_text(message)
        spoken = plain_text(det.get("spoken_message") or self.cfg.tts_alert_message or f"{clean_title}. {clean_msg}")
        return {
            "event": event or "feed_clog_detected",
            "source": "molly_cody_feeder_watchdog",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "urgent": bool(self._urgent),
            "clog_detected": bool(clog_flag),
            "food_dispensed": fed_flag,
            "title": clean_title,
            "message": clean_msg,
            "spoken_message": spoken,
            "details": det,
        }

    def _send_home_assistant(
        self,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        event: str = "",
    ) -> bool:
        payload = self._build_ha_payload(title, message, details, event=event)
        try:
            resp = requests.post(self.cfg.ha_webhook_url, json=payload, timeout=10)
            if resp.status_code >= 400:
                return False
            return True
        except Exception as exc:
            logger.error("Failed to send Home Assistant webhook: %s", exc)
            return False

    def _send_home_assistant_tts(
        self,
        title: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        event: str = "",
    ) -> bool:
        if not self.cfg.ha_tts_webhook_url:
            return False
        payload = self._build_ha_payload(title, message, details, event=event)
        try:
            resp = requests.post(self.cfg.ha_tts_webhook_url, json=payload, timeout=10)
            return resp.status_code < 400
        except Exception as exc:
            logger.error("Failed to send Home Assistant TTS webhook: %s", exc)
            return False

    def _send_discord(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            color = 0xFF2E2E if self._urgent else 0x3AA655
            payload = {"embeds": [{"title": title, "description": message, "color": color}]}
            if image_path and Path(image_path).exists():
                with open(image_path, "rb") as img_file:
                    requests.post(
                        self.cfg.discord_webhook_url,
                        data={"payload_json": requests.compat.json.dumps(payload) if hasattr(requests, "compat") else __import__("json").dumps(payload)},
                        files={"file": (Path(image_path).name, img_file, "image/jpeg")},
                        timeout=15,
                    )
            else:
                requests.post(self.cfg.discord_webhook_url, json=payload, timeout=10)
        except Exception as exc:
            logger.error("Failed to send Discord alert: %s", exc)

    def _send_telegram(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            safe_title = html.escape(title)
            safe_msg = html.escape(plain_text(message))
            caption = f"<b>{safe_title}</b>\n\n{safe_msg}"[:1024]
            base_url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"
            if image_path and Path(image_path).exists():
                with open(image_path, "rb") as img_file:
                    requests.post(
                        f"{base_url}/sendPhoto",
                        data={"chat_id": self.cfg.telegram_chat_id, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": img_file},
                        timeout=15,
                    )
            else:
                requests.post(
                    f"{base_url}/sendMessage",
                    json={"chat_id": self.cfg.telegram_chat_id, "text": caption, "parse_mode": "HTML"},
                    timeout=10,
                )
        except Exception as exc:
            logger.error("Failed to send Telegram alert: %s", exc)

    def _send_ntfy(self, title: str, message: str, image_path: Optional[str]) -> None:
        try:
            headers = {
                "Title": plain_text(title),
                "Priority": "urgent" if self._urgent else "default",
                "Tags": "dog,warning,rotating_light" if self._urgent else "dog,Bowl",
            }
            if image_path and Path(image_path).exists():
                headers["Filename"] = Path(image_path).name
                with open(image_path, "rb") as img_file:
                    requests.post(self.cfg.ntfy_topic_url, data=img_file, headers=headers, timeout=15)
            else:
                requests.post(
                    self.cfg.ntfy_topic_url,
                    data=plain_text(message).encode("utf-8"),
                    headers=headers,
                    timeout=10,
                )
        except Exception as exc:
            logger.error("Failed to send ntfy alert: %s", exc)
