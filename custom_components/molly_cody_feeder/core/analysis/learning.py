"""Portion-aware dispense anomalies and user-feedback similarity check."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from custom_components.molly_cody_feeder.core.analysis.results import (
    AudioAnalysisResult,
)

DEFAULT_SEC_PER_PORTION = 2.3
DEFAULT_KIBBLE_RATE = 13.0
DEFAULT_WEAK_FLOW_TING_RATE = 4.6
WEAK_FLOW_MIN_MOTOR_SEC = 8.0


def assess_dispense(
    audio: Optional[AudioAnalysisResult],
    portions: int,
    baselines: Optional[Dict[str, Optional[float]]] = None,
    weak_flow_ting_rate: float = DEFAULT_WEAK_FLOW_TING_RATE,
    recording_truncated: bool = False,
) -> Dict[str, Any]:
    base = baselines or {}
    sec_per_p = float(base.get("motor_sec_per_portion") or DEFAULT_SEC_PER_PORTION)
    norm_rate = float(base.get("kibble_rate") or DEFAULT_KIBBLE_RATE)
    expected_sec = max(1.5, portions * sec_per_p)
    if audio is None or not audio.motor_detected:
        return {
            "short_run": False,
            "low_flow": False,
            "weak_flow": False,
            "expected_motor_sec": round(expected_sec, 2),
            "baseline_kibble_rate": round(norm_rate, 2),
            "note": "",
        }
    short_run = (
        not recording_truncated
        and portions >= 2
        and audio.motor_duration_sec < max(1.5, 0.55 * expected_sec)
    )
    low_flow = (
        not recording_truncated
        and audio.motor_duration_sec >= 2.0
        and audio.kibble_rate_per_sec < 0.45 * norm_rate
    )
    weak_flow = (
        not recording_truncated
        and audio.kibble_dispensed_audio
        and audio.motor_duration_sec >= WEAK_FLOW_MIN_MOTOR_SEC
        and 0 < audio.ting_rate_per_sec < weak_flow_ting_rate
    )
    notes = []
    if short_run:
        notes.append(
            f"Motor ran only {audio.motor_duration_sec:.1f}s for {portions} portions (expected ~{expected_sec:.1f}s)"
        )
    if low_flow:
        notes.append(
            f"Kibble rattle rate {audio.kibble_rate_per_sec:.1f}/s is well below normal ({norm_rate:.1f}/s)"
        )
    if weak_flow:
        notes.append(
            f"Weak kibble flow ({audio.ting_rate_per_sec:.1f} tings/s vs normal >= {weak_flow_ting_rate:.1f}/s): "
            "hopper may be getting low or partially bridged"
        )
    return {
        "short_run": short_run,
        "low_flow": low_flow,
        "weak_flow": weak_flow,
        "expected_motor_sec": round(expected_sec, 2),
        "baseline_kibble_rate": round(norm_rate, 2),
        "note": "; ".join(notes),
    }


def audio_features(audio: AudioAnalysisResult) -> List[float]:
    return [
        float(audio.motor_duration_sec),
        float(audio.kibble_impact_peaks),
        float(audio.kibble_rate_per_sec),
        float(audio.impact_energy_db),
        float(getattr(audio, "ting_rate_per_sec", 0.0)),
    ]


def feedback_suggests_false_alarm(
    audio: Optional[AudioAnalysisResult],
    labeled_rows: List[Dict[str, Any]],
    k: int = 3,
) -> Optional[str]:
    if audio is None or len(labeled_rows) < k:
        return None
    scales = (3.0, 20.0, 6.0, 8.0)
    feats = audio_features(audio)[:4]
    target = [f / s for f, s in zip(feats, scales)]

    scored = []
    for r in labeled_rows:
        raw_vec = (
            float(r.get("motor_duration_sec") or 0.0),
            float(r.get("kibble_peaks") or 0.0),
            float(r.get("kibble_rate") or 0.0),
            float(r.get("impact_energy_db") or 0.0),
        )
        dist = math.sqrt(sum(((t - (v / s)) ** 2) for t, v, s in zip(target, raw_vec, scales)))
        true_ok = bool(r["success"]) if r["user_feedback"] == "right" else not bool(r["success"])
        scored.append((dist, true_ok, int(r["id"])))

    scored.sort(key=lambda x: x[0])
    nearest = [s for s in scored[:k] if s[0] < 1.25]
    if len(nearest) >= 2 and all(s[1] for s in nearest):
        ids = ", ".join(f"#{s[2]}" for s in nearest)
        return f"Audio closely matches past feeds you marked OK ({ids}); overriding false clog alarm."
    return None
