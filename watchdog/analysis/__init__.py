"""Bowl / dispense analysis package.

    results         AudioAnalysisResult, VisualAnalysisResult
    audio           AudioAnalysisMixin   motor window vs. kibble impacts
    vision          VisionMixin          daylight fill, IR change detection, dog at bowl
    llm             LlmVisionMixin       optional Gemini double-check
    stream_capture  StreamCaptureMixin   ffmpeg capture from external streams
    learning        assess_dispense, learned audio model
    bowl_analyzer   BowlAnalyzer facade
"""
from watchdog.analysis.bowl_analyzer import BowlAnalyzer
from watchdog.analysis.learning import (
    AUDIO_MODEL_FEATURES,
    DEFAULT_KIBBLE_RATE,
    DEFAULT_MOTOR_SEC_PER_PORTION,
    assess_dispense,
    audio_features,
)
from watchdog.analysis.results import AudioAnalysisResult, VisualAnalysisResult

__all__ = [
    "AUDIO_MODEL_FEATURES",
    "DEFAULT_KIBBLE_RATE",
    "DEFAULT_MOTOR_SEC_PER_PORTION",
    "AudioAnalysisResult",
    "BowlAnalyzer",
    "VisualAnalysisResult",
    "assess_dispense",
    "audio_features",
]
