"""Kibble flow over time: when kibble was actually falling during the motor run.

Owner's observation (confirmed on 29 hand-checked feeds): the motor is a sustained broadband jump, and while
pellets are falling the whole 600-3900 Hz band is lifted by their impacts. When the flow stops (jam, empty hopper)
the motor keeps humming but the spectrum goes clearly darker. So a "gap" is the smoothed 600-3900 Hz level dropping
well below the motor's typical level for a while. Kibble coverage = share of the motor run with kibble falling:
normal feeds ~75-99 %, partial jams / emptying hopper ~20-70 %.

Pure numpy (runs on the VPS). Mirrors the suggestion logic of ml/review_bundle.html.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

N_FFT, HOP = 256, 64          # 32 ms window, 8 ms hop at 8 kHz
SMOOTH_SEC = 0.5              # level smoothing
GAP_DB = 8.0                  # a gap is this far below the motor's typical level ...
GAP_MIN_SEC = 0.8             # ... for at least this long
LEAD_SEC = 0.3                # first pellets land ~0.3 s after the motor starts
TYPICAL_PCT = 60              # "typical" motor-run level (robust to gaps)


@dataclass
class KibbleFlow:
    coverage: float                      # kibble time / motor time (0..1)
    kibble_sec: float
    longest_gap_sec: float
    tail_gap_sec: float                  # motor kept running after the last kibble (empty-hopper signature)
    gaps: List[Tuple[float, float]] = field(default_factory=list)      # seconds from recording start
    segments: List[Tuple[float, float]] = field(default_factory=list)  # kibble falling


def _band_level(samples: np.ndarray, sample_rate: int) -> Tuple[np.ndarray, float]:
    n = (len(samples) - N_FFT) // HOP
    if n <= 4:
        return np.zeros(0, dtype=np.float32), HOP / float(sample_rate)
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(samples[idx] * np.hanning(N_FFT), axis=1)) ** 2
    f = np.fft.rfftfreq(N_FFT, 1.0 / sample_rate)
    band = (f >= 600) & (f <= 3900)
    level = (10.0 * np.log10(spec[:, band] + 1e-12)).mean(axis=1)
    return level.astype(np.float32), HOP / float(sample_rate)


def _runs(mask: np.ndarray, dt: float, merge_sec: float, min_sec: float) -> List[Tuple[float, float]]:
    out: List[List[int]] = []
    start = -1
    for i, v in enumerate(np.append(mask, False)):
        if v and start < 0:
            start = i
        elif not v and start >= 0:
            if out and (start - out[-1][1]) * dt <= merge_sec:
                out[-1][1] = i - 1
            else:
                out.append([start, i - 1])
            start = -1
    return [(s * dt, (e + 1) * dt) for s, e in out if (e - s + 1) * dt >= min_sec]


def kibble_flow(
    samples: np.ndarray,
    sample_rate: int,
    motor_start_sec: float,
    motor_end_sec: float,
    gap_db: float = GAP_DB,
    gap_min_sec: float = GAP_MIN_SEC,
) -> Optional[KibbleFlow]:
    """Kibble flow inside the motor run [motor_start_sec, motor_end_sec]; None if it can't be measured."""
    motor_sec = motor_end_sec - motor_start_sec
    if motor_sec < 1.0:
        return None
    level, dt = _band_level(samples, sample_rate)
    if len(level) == 0:
        return None
    w = max(1, int(round(SMOOTH_SEC / dt)))
    smooth = np.convolve(level, np.ones(w, dtype=np.float32) / w, mode="full")[: len(level)]
    smooth[: w - 1] = np.cumsum(level[: w - 1]) / np.arange(1, w)  # same causal ramp as the review tool
    lag = SMOOTH_SEC / 2.0
    c0 = int(np.ceil(motor_start_sec / dt))
    c1 = min(len(smooth) - 1, int(np.floor(motor_end_sec / dt)))
    if c1 - c0 < 10:
        return None
    seg = smooth[c0: c1 + 1]
    typical = float(np.percentile(seg, TYPICAL_PCT))
    gaps = [(motor_start_sec + s - lag, motor_start_sec + e - lag)
            for s, e in _runs(seg < typical - gap_db, dt, 0.2, gap_min_sec)]
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
    return KibbleFlow(
        coverage=round(min(1.0, kib / motor_sec), 3),
        kibble_sec=round(kib, 2),
        longest_gap_sec=round(max([e - s for s, e in gaps], default=0.0), 2),
        tail_gap_sec=round(max(0.0, motor_end_sec - last), 2),
        gaps=[(round(s, 2), round(e, 2)) for s, e in gaps],
        segments=[(round(s, 2), round(e, 2)) for s, e in kibble],
    )
