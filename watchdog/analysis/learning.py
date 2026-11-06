"""Learned / adaptive pieces: dispense early-warning flags and the optional label-trained audio model."""

import json
from typing import Optional

import numpy as np

from watchdog.paths import DATA_DIR
from watchdog.analysis.results import AudioAnalysisResult


AUDIO_MODEL_FEATURES = ["motor_sec", "peaks", "rate", "impact_db", "ting_rate"]


_MODEL_CACHE: dict = {}


def audio_features(a: "AudioAnalysisResult") -> list:
    return [a.motor_duration_sec, float(a.kibble_impact_peaks), a.kibble_rate_per_sec, a.impact_energy_db,
            a.ting_rate_per_sec]


def _model_prob(a: "AudioAnalysisResult") -> Optional[float]:
    """Applies data/audio_model.json if present (tiny logistic regression; never authoritative)."""
    path = DATA_DIR / "audio_model.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _MODEL_CACHE.get("mtime") != mtime:
        try:
            _MODEL_CACHE.update(json.loads(path.read_text()), mtime=mtime)
        except Exception:
            return None
    try:
        x = (np.array(audio_features(a)) - np.array(_MODEL_CACHE["mean"])) / np.array(_MODEL_CACHE["std"])
        z = float(x @ np.array(_MODEL_CACHE["w"]) + _MODEL_CACHE["b"])
        return round(1.0 / (1.0 + np.exp(-z)), 3)
    except Exception:
        return None


# Learned from the labeled clips: run time scales ~linearly with portions (1p 2.7 s, 5p 12 s, 15p 35 s)
DEFAULT_MOTOR_SEC_PER_PORTION = 2.3


DEFAULT_KIBBLE_RATE = 13.0


def assess_dispense(
    audio: "AudioAnalysisResult",
    portions: int,
    motor_sec_per_portion: float = DEFAULT_MOTOR_SEC_PER_PORTION,
    baseline_rate: float = DEFAULT_KIBBLE_RATE,
    recording_truncated: bool = False,
    weak_flow_ting_rate: float = 4.6,
) -> dict:
    """
    Early-warning flags on top of the pass/fail verdict (never flips a pass into a fail):
    - short_run: motor stopped well before the expected run time for `portions` (jam / partial dispense)
    - low_flow:  kibble impacts per motor-second far below this feeder's normal rate
                 (partial clog, hopper running low, or an overfull bowl muffling impacts)
    """
    flags = {"short_run": False, "low_flow": False, "weak_flow": False, "expected_motor_sec": None}
    if not audio or not audio.motor_detected:
        return flags
    if portions and portions > 0:
        expected = portions * motor_sec_per_portion
        flags["expected_motor_sec"] = round(expected, 1)
        if not recording_truncated and portions >= 2 and audio.motor_duration_sec < 0.6 * expected:
            flags["short_run"] = True
    if audio.kibble_dispensed_audio and audio.motor_duration_sec >= 4.0:
        flags["low_flow"] = audio.kibble_rate_per_sec < 0.4 * baseline_rate
    # Weak kibble flow (v0.6, from 26 hand-labelled feeds): every feed the owner marked as a partial jam had
    # < WEAK_FLOW_TING_RATE tings per motor-second (3.2-4.6/s), normal feeds mostly 4.6-8.4/s. Soft warning only.
    if (audio.kibble_dispensed_audio and audio.motor_duration_sec >= 8.0 and not recording_truncated
            and audio.ting_count > 0 and audio.ting_rate_per_sec < weak_flow_ting_rate):
        flags["weak_flow"] = True
    return flags
