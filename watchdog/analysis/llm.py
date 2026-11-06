"""Optional Gemini vision double-check of the bowl images."""

import base64
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("poor_puppy.analysis.llm")


class LlmVisionMixin:
    """Asks a vision LLM whether kibble is present / increased (never authoritative alone)."""

    def _verify_with_gemini(
        self, after_image_path: str, before_image_path: Optional[str] = None
    ) -> Tuple[Optional[bool], str]:
        try:
            parts = [
                {
                    "text": (
                        "You are monitoring an automatic dog feeder bowl for a Bernese Mountain Dog puppy. "
                        "If two images are provided, the first is BEFORE the scheduled feed and the second is AFTER. "
                        "Determine if dog food kibble is present in the bowl and whether the amount of food increased. "
                        'Respond ONLY with valid JSON: {"food_present": bool, "food_increased": bool, '
                        '"fill_level_percent": int, "reasoning": "short explanation"}'
                    )
                }
            ]
            if before_image_path and Path(before_image_path).exists():
                with open(before_image_path, "rb") as bf:
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(bf.read()).decode("utf-8")}})
            with open(after_image_path, "rb") as af:
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(af.read()).decode("utf-8")}})

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.cfg.vision_model}:generateContent"
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            # Key goes in a header so it never appears in URLs, logs or exception messages
            resp = requests.post(url, json=payload, headers={"x-goog-api-key": self.cfg.gemini_api_key}, timeout=15)
            resp.raise_for_status()
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return bool(parsed.get("food_present")), str(parsed.get("reasoning", ""))
        except Exception as exc:
            logger.warning("Gemini vision check skipped/failed: %s", exc)
            return None, f"LLM check error: {exc}"
