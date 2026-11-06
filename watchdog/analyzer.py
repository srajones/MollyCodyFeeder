"""Backward-compatible import path. The implementation lives in the `watchdog.analysis` package."""
from watchdog.analysis import (  # noqa: F401
    AUDIO_MODEL_FEATURES,
    DEFAULT_KIBBLE_RATE,
    DEFAULT_MOTOR_SEC_PER_PORTION,
    AudioAnalysisResult,
    BowlAnalyzer,
    VisualAnalysisResult,
    assess_dispense,
    audio_features,
)
from watchdog.analysis.learning import _model_prob  # noqa: F401
