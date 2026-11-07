"""Optional Gemini multimodal vision second-opinion helper (`LlmVisionMixin`)."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("molly_cody_feeder.analyzer")


class LlmVisionMixin:
    """Optional Gemini multimodal vision check."""

    def _verify_with_gemini(
        self, after_path: str, before_path: Optional[str] = None
    ) -> Tuple[Optional[bool], str]:
        try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.cfg.vision_model}:generateContent"
            )
            parts = []
            if before_path and Path(before_path).exists():
                b64_before = base64.b64encode(Path(before_path).read_bytes()).decode("utf-8")
                parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_before}})

            b64_after = base64.b64encode(Path(after_path).read_bytes()).decode("utf-8")
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_after}})

            prompt = (
                "You are inspecting a pet feeder camera (Molly & Cody Smart Feeder). "
                "Determine whether dry dog kibble is present in the feeding bowl in the latest image "
                "(and whether new kibble was dispensed if a before image is also provided). "
                'Respond ONLY with valid JSON: {"food_present": true/false, "reasoning": "short explanation"}'
            )
            parts.append({"text": prompt})

            resp = requests.post(
                url,
                headers={"x-goog-api-key": self.cfg.gemini_api_key},
                json={
                    "contents": [{"parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return bool(parsed.get("food_present")), str(parsed.get("reasoning", ""))
        except Exception as exc:
            logger.warning("Gemini multimodal vision check failed: %s", exc)
            return None, f"LLM check error: {exc}"
