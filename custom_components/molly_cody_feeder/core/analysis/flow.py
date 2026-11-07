"""Kibble flow over time: when kibble was actually falling during the motor run.

Includes both a chunked zero-copy NumPy STFT path (< 500 KB peak RAM) and a fast
pure-Python stdlib fallback (`_analyze_flow_stdlib` using in-place 32-pt Radix-2 FFT).
"""
from __future__ import annotations

import array
import math
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

from custom_components.molly_cody_feeder.core.analysis.ting import (
    _HANN_32,
    _fft32_power_into,
    _to_stdlib_array,
)

HAS_NUMPY: bool = np is not None

N_FFT, HOP = 256, 64  # 32 ms window, 8 ms hop at 8 kHz
HOP_STD = 160         # 20 ms hop in pure-Python stdlib fallback
SMOOTH_SEC = 0.5
GAP_DB = 8.0
GAP_MIN_SEC = 0.8
LEAD_SEC = 0.3
TYPICAL_PCT = 60


@dataclass
class FlowWindow:
    coverage: float
    kibble_sec: float
    motor_sec: float = 0.0
    longest_gap_sec: float = 0.0
    tail_gap_sec: float = 0.0
    gaps: List[Tuple[float, float]] = field(default_factory=list)
    segments: List[Tuple[float, float]] = field(default_factory=list)


KibbleFlow = FlowWindow


def _runs_from_bools(mask: Sequence[bool], dt: float, merge_sec: float, min_sec: float) -> List[Tuple[float, float]]:
    out: List[List[int]] = []
    start = -1
    for i, v in enumerate(list(mask) + [False]):
        if v and start < 0:
            start = i
        elif not v and start >= 0:
            if out and (start - out[-1][1]) * dt <= merge_sec:
                out[-1][1] = i - 1
            else:
                out.append([start, i - 1])
            start = -1
    return [(s * dt, (e + 1) * dt) for s, e in out if (e - s + 1) * dt >= min_sec]


def _percentile_stdlib(vals: Sequence[float], pct: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return float(s[0])
    pos = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(len(s) - 1, lo + 1)
    frac = pos - lo
    return float(s[lo] + frac * (s[hi] - s[lo]))


def _compute_flow_from_level(
    level: Sequence[float],
    dt: float,
    motor_start_sec: float,
    motor_end_sec: float,
    gap_db: float = GAP_DB,
    gap_min_sec: float = GAP_MIN_SEC,
) -> Optional[FlowWindow]:
    motor_sec = motor_end_sec - motor_start_sec
    if motor_sec < 1.0 or len(level) == 0:
        return None
    n = len(level)
    w = max(1, int(round(SMOOTH_SEC / dt)))
    smooth = array.array("f", [0.0] * n)
    running = 0.0
    for i in range(n):
        running += level[i]
        if i < w - 1:
            smooth[i] = running / float(i + 1)
        else:
            if i >= w:
                running -= level[i - w]
            smooth[i] = running / float(w)

    lag = SMOOTH_SEC / 2.0
    c0 = int(math.ceil(motor_start_sec / dt))
    c1 = min(n - 1, int(math.floor(motor_end_sec / dt)))
    if c1 - c0 < 6:
        return None
    seg = smooth[c0 : c1 + 1]
    typical = _percentile_stdlib(seg, TYPICAL_PCT)
    thresh = typical - gap_db
    mask = [val < thresh for val in seg]
    gaps = [
        (motor_start_sec + s - lag, motor_start_sec + e - lag)
        for s, e in _runs_from_bools(mask, dt, 0.2, gap_min_sec)
    ]
    kibble: List[Tuple[float, float]] = []
    t = motor_start_sec + LEAD_SEC
    for gs, ge in gaps:
        if gs - t >= 0.4:
            kibble.append((t, gs))
        t = max(t, ge)
    if motor_end_sec - t >= 0.4:
        kibble.append((t, motor_end_sec))
    kib = sum(e - s for s, e in kibble)
    last = kibble[-1][1] if kibble else motor_start_sec
    return FlowWindow(
        coverage=round(min(1.0, kib / motor_sec), 3),
        kibble_sec=round(kib, 2),
        motor_sec=round(motor_sec, 2),
        longest_gap_sec=round(max([e - s for s, e in gaps], default=0.0), 2),
        tail_gap_sec=round(max(0.0, motor_end_sec - last), 2),
        gaps=[(round(s, 2), round(e, 2)) for s, e in gaps],
        segments=[(round(s, 2), round(e, 2)) for s, e in kibble],
    )


def _analyze_flow_stdlib(
    samples: Any,
    sample_rate: int,
    motor_start_sec: float,
    motor_end_sec: float,
    gap_db: float = GAP_DB,
    gap_min_sec: float = GAP_MIN_SEC,
) -> Optional[FlowWindow]:
    """Pure-Python stdlib kibble flow analyzer using in-place 32-pt Radix-2 FFT (zero numpy)."""
    motor_sec = motor_end_sec - motor_start_sec
    if samples is None or sample_rate <= 0 or motor_sec < 1.0:
        return None
    arr, scale = _to_stdlib_array(samples)
    n_samp = len(arr)
    n_frames = (n_samp - 32) // HOP_STD
    if n_frames <= 4:
        return None

    h = [w * scale for w in _HANN_32]
    df = float(sample_rate) / 32.0
    band_bins = [k for k in range(17) if 600.0 <= k * df <= 3900.0]
    if not band_bins:
        return None
    b_start = band_bins[0]
    b_end = band_bins[-1] + 1
    inv_b = 1.0 / float(b_end - b_start)

    dt = HOP_STD / float(sample_rate)
    w = max(1, int(round(SMOOTH_SEC / dt)))
    f_start = max(0, int(math.floor(motor_start_sec / dt)) - w - 2)
    f_end = min(n_frames, int(math.ceil(motor_end_sec / dt)) + 4)

    level = array.array("f", [0.0] * n_frames)
    re = [0.0] * 32
    im = [0.0] * 32
    spec = [0.0] * 17
    _log10 = math.log10

    for fi in range(f_start, f_end):
        _fft32_power_into(arr, fi * HOP_STD, h, re, im, spec)
        s_db = 0.0
        for k in range(b_start, b_end):
            s_db += 10.0 * _log10(spec[k] + 1e-12)
        level[fi] = s_db * inv_b

    return _compute_flow_from_level(level, dt, motor_start_sec, motor_end_sec, gap_db=gap_db, gap_min_sec=gap_min_sec)


def _band_level_numpy(samples: Any, sample_rate: int) -> Tuple[Any, float]:
    n = (len(samples) - N_FFT) // HOP
    dt = HOP / float(sample_rate)
    if n <= 4:
        return np.zeros(0, dtype=np.float32), dt
    scale = (1.0 / 32768.0) if samples.dtype == np.int16 else 1.0
    win = (np.hanning(N_FFT) * scale).astype(np.float32)
    f = np.fft.rfftfreq(N_FFT, 1.0 / sample_rate)
    band = (f >= 600) & (f <= 3900)
    stride = samples.strides[0]
    frames = np.lib.stride_tricks.as_strided(
        samples, shape=(n, N_FFT), strides=(HOP * stride, stride)
    )
    level = np.empty(n, dtype=np.float32)
    batch = 64
    for s in range(0, n, batch):
        e = min(s + batch, n)
        spec = np.abs(np.fft.rfft(frames[s:e] * win, axis=1)).astype(np.float32)
        spec *= spec
        level[s:e] = (10.0 * np.log10(spec[:, band] + 1e-12)).mean(axis=1)
    return level, dt


def kibble_flow(
    samples: Any,
    sample_rate: int,
    motor_start_sec: float,
    motor_end_sec: float,
    win_sec: float = 0.25,
    gap_db: float = GAP_DB,
    gap_min_sec: float = GAP_MIN_SEC,
) -> Optional[FlowWindow]:
    """Kibble flow inside the motor run [motor_start_sec, motor_end_sec]; None if it can't be measured."""
    motor_sec = motor_end_sec - motor_start_sec
    if samples is None or sample_rate <= 0 or motor_sec < 1.0:
        return None
    if not HAS_NUMPY or np is None or not isinstance(samples, np.ndarray):
        return _analyze_flow_stdlib(
            samples, sample_rate, motor_start_sec, motor_end_sec, gap_db=gap_db, gap_min_sec=gap_min_sec
        )
    level, dt = _band_level_numpy(samples, sample_rate)
    if len(level) == 0:
        return None
    return _compute_flow_from_level(level, dt, motor_start_sec, motor_end_sec, gap_db=gap_db, gap_min_sec=gap_min_sec)
