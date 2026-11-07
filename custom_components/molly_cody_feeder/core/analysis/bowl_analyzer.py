"""BowlAnalyzer: combines acoustic anti-clog analysis and optional visual bowl inspection."""
from __future__ import annotations

from typing import Any, Dict, Optional

from custom_components.molly_cody_feeder.core.analysis.audio import AudioMixin
from custom_components.molly_cody_feeder.core.analysis.learning import assess_dispense
from custom_components.molly_cody_feeder.core.analysis.llm import LlmVisionMixin
from custom_components.molly_cody_feeder.core.analysis.results import (
    AudioAnalysisResult,
)
from custom_components.molly_cody_feeder.core.analysis.vision import VisionMixin
from custom_components.molly_cody_feeder.core.config import RuntimeConfig


class BowlAnalyzer(AudioMixin, VisionMixin, LlmVisionMixin):
    """Performs acoustic dispense verification and before/after visual bowl fill comparison."""

    def __init__(self, config: Optional[RuntimeConfig] = None):
        self.cfg = config or RuntimeConfig()

    def verify_audio_only(
        self,
        wav_path: str,
        portions: int = 1,
        baselines: Optional[Dict[str, Optional[float]]] = None,
    ) -> Dict[str, Any]:
        """Runs ultra-lightweight audio-only dispense verification without touching vision/opencv/PIL."""
        audio_res: AudioAnalysisResult = self.analyze_dispense_audio(wav_path)
        anomaly = assess_dispense(
            audio_res,
            portions=portions,
            baselines=baselines,
            weak_flow_ting_rate=float(getattr(self.cfg, "weak_flow_ting_rate", 4.6)),
        )
        if audio_res.kibble_dispensed_audio:
            outcome = "dispensed"
            passed = True
            clog_detected = False
        elif audio_res.motor_detected:
            outcome = "motor_no_kibble"
            passed = False
            clog_detected = True
        else:
            outcome = "no_motor"
            passed = False
            clog_detected = True
        return {
            "passed": passed,
            "outcome": outcome,
            "clog_detected": clog_detected,
            "audio": audio_res,
            "anomaly": anomaly,
        }
