"""Result data classes returned by the audio and vision analyzers."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioAnalysisResult:
    motor_detected: bool
    kibble_impact_peaks: int
    kibble_dispensed_audio: bool
    motor_energy_db: float
    impact_energy_db: float
    diagnosis: str
    motor_duration_sec: float = 0.0
    # Kibble impacts per second of motor run time (normal ~10-15/s; clog ~3/s)
    kibble_rate_per_sec: float = 0.0
    # Kibble "ting" onsets (pellets hitting the bowl) and their rate per second of motor run
    ting_count: int = 0
    ting_rate_per_sec: float = 0.0
    # Optional learned model (scripts/calibrate.py): probability that kibble came out
    ml_prob_dispensed: Optional[float] = None
    # Motor run (seconds from the start of the recording) and kibble flow inside it (watchdog/analysis/flow.py)
    motor_start_sec: Optional[float] = None
    motor_end_sec: Optional[float] = None
    kibble_coverage: Optional[float] = None      # share of the motor run with kibble falling (normal ~0.75-1.0)
    longest_gap_sec: Optional[float] = None      # longest stretch with the motor running but no kibble
    tail_gap_sec: Optional[float] = None         # motor kept running after the last kibble (empty hopper)
    kibble_gaps: Optional[list] = None           # [(start, end), ...] seconds


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
