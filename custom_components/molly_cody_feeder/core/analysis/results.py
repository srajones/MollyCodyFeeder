"""Analysis result dataclasses for the Molly & Cody Smart Feeder."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class AudioAnalysisResult:
    motor_detected: bool
    kibble_impact_peaks: int
    kibble_dispensed_audio: bool
    motor_energy_db: float
    impact_energy_db: float
    diagnosis: str
    motor_duration_sec: float = 0.0
    kibble_rate_per_sec: float = 0.0
    ting_count: int = 0
    ting_rate_per_sec: float = 0.0
    ml_prob_dispensed: Optional[float] = None
    motor_start_sec: Optional[float] = None
    motor_end_sec: Optional[float] = None
    kibble_coverage: Optional[float] = None
    longest_gap_sec: Optional[float] = None
    tail_gap_sec: Optional[float] = None
    kibble_gaps: Optional[List[Any]] = None
    flow_coverage: float = 0.0
    flow_segments: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class VisualAnalysisResult:
    before_fill_ratio: float
    after_fill_ratio: float
    fill_delta_ratio: float
    frame_diff_ratio: float
    bowl_empty: bool
    food_increased: bool
    food_present_cv: bool
    food_present_llm: Optional[bool]
    llm_reasoning: str
    annotated_image_path: str
    diagnosis: str
    is_infrared_mode: bool = False
    dog_at_bowl_detected: bool = False
