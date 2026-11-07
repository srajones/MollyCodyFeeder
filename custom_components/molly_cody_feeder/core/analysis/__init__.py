"""Acoustic and visual bowl analysis subpackage."""
from __future__ import annotations

from custom_components.molly_cody_feeder.core.analysis.audio import (
    HAS_NUMPY,
    AudioAnalysisMixin,
    AudioMixin,
    _analyze_audio_stdlib,
)
from custom_components.molly_cody_feeder.core.analysis.bowl_analyzer import (
    BowlAnalyzer,
)
from custom_components.molly_cody_feeder.core.analysis.flow import (
    FlowWindow,
    KibbleFlow,
    _analyze_flow_stdlib,
    kibble_flow,
)
from custom_components.molly_cody_feeder.core.analysis.learning import (
    assess_dispense,
    audio_features,
    feedback_suggests_false_alarm,
)
from custom_components.molly_cody_feeder.core.analysis.results import (
    AudioAnalysisResult,
    VisualAnalysisResult,
)
from custom_components.molly_cody_feeder.core.analysis.ting import (
    _analyze_tings_stdlib,
    ting_times,
)

__all__ = [
    "HAS_NUMPY",
    "AudioAnalysisResult",
    "VisualAnalysisResult",
    "BowlAnalyzer",
    "AudioMixin",
    "AudioAnalysisMixin",
    "FlowWindow",
    "KibbleFlow",
    "kibble_flow",
    "_analyze_audio_stdlib",
    "_analyze_tings_stdlib",
    "_analyze_flow_stdlib",
    "assess_dispense",
    "audio_features",
    "feedback_suggests_false_alarm",
    "ting_times",
]
