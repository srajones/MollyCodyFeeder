"""Audio-first feed verdict & outcome classifier (`decision.py`)."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from custom_components.molly_cody_feeder.core.analysis import (
    AudioAnalysisResult,
    VisualAnalysisResult,
)

DISPENSED = "dispensed"
MOTOR_NO_KIBBLE = "motor_no_kibble"
NO_MOTOR = "no_motor"
UNVERIFIED = "unverified"
CAMERA_UNAVAILABLE = "camera_unavailable"
RECORDING_CUT_SHORT = "recording_cut_short"
BOWL_CHECK = "bowl_check"

DEFAULT_MIN_RECORDING_COVERAGE = 0.75
DEFAULT_MAX_TAIL_GAP_SEC = 5.0


def recording_cut_short(
    session_info: Optional[Dict[str, Any]],
    min_coverage: float = DEFAULT_MIN_RECORDING_COVERAGE,
    max_tail_gap_sec: float = DEFAULT_MAX_TAIL_GAP_SEC,
) -> bool:
    if not isinstance(session_info, dict):
        return False
    wall = session_info.get("recorded_wall_sec")
    audio = session_info.get("audio_sec")
    if wall is None or audio is None:
        return False
    try:
        wall_f, audio_f = float(wall), float(audio)
    except (TypeError, ValueError):
        return False
    if wall_f < 3.0:
        return False
    if audio_f < min_coverage * wall_f:
        return True
    last_rx = session_info.get("audio_last_rx_sec")
    if last_rx is not None:
        try:
            if (wall_f - float(last_rx)) > max_tail_gap_sec:
                return True
        except (TypeError, ValueError):
            pass
    return False


def decide_passed(
    audio_res: Optional[AudioAnalysisResult],
    visual_res: Optional[VisualAnalysisResult],
    feed_abnormal: int,
    expect_dispense: bool,
    vision_can_override_audio: bool = False,
) -> bool:
    if feed_abnormal > 1:
        return False
    if not expect_dispense:
        if visual_res is None:
            return False
        return bool(visual_res.food_present_cv or visual_res.food_present_llm is True)

    if audio_res is not None:
        if audio_res.kibble_dispensed_audio:
            return True
        if vision_can_override_audio and visual_res is not None:
            if visual_res.food_increased or visual_res.food_present_llm is True:
                return True
        return False

    if visual_res is not None:
        return bool(visual_res.food_increased or visual_res.food_present_llm is True)
    return False


def classify(
    audio_res: Optional[AudioAnalysisResult],
    visual_res: Optional[VisualAnalysisResult],
    expect_dispense: bool,
    passed: bool,
    cut_short: bool = False,
) -> Tuple[str, bool]:
    if not expect_dispense:
        return BOWL_CHECK, passed
    if audio_res and audio_res.kibble_dispensed_audio:
        return DISPENSED, passed
    if cut_short:
        return RECORDING_CUT_SHORT, False
    if audio_res and audio_res.motor_detected:
        return MOTOR_NO_KIBBLE, passed
    if audio_res:
        return NO_MOTOR, passed
    if visual_res is not None:
        return UNVERIFIED, passed
    return CAMERA_UNAVAILABLE, False


def is_clog(outcome: str, passed: bool) -> bool:
    return not passed and outcome in (MOTOR_NO_KIBBLE, NO_MOTOR)


def is_unverified(outcome: str) -> bool:
    return outcome in (RECORDING_CUT_SHORT, CAMERA_UNAVAILABLE)


def status_label(outcome: str, passed: bool) -> str:
    if passed:
        return "ok"
    if is_unverified(outcome):
        return "unverified"
    return "clog_detected"
