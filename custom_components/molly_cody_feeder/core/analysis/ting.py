"""Kibble 'ting' detector: sharp impacts of pellets on the steel bowl / kibble pile.

Includes both a chunked zero-copy NumPy STFT path (< 500 KB peak RAM) and a fast
pure-Python stdlib fallback (`_analyze_tings_stdlib` using in-place 32-pt Radix-2 FFT
on `array.array` with zero per-frame list allocations).
"""
from __future__ import annotations

import array
import math
from typing import Any, Dict, List, Sequence, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

HAS_NUMPY: bool = np is not None

WIN, HOP = 256, 80  # 32 ms window, 10 ms hop at 8 kHz
TING_SPECTRAL_RATIO_MIN = 1.6  # (600-1.5k + >2.5k) / (1.5-2.5k + <600) at the onset
ONSET_MAD_K = 4.0  # onset = spectral flux above median + K * MAD
MIN_SPACING_HOPS = 3  # 30 ms between counted tings

_HANN_32: Tuple[float, ...] = tuple(
    0.5 * (1.0 - math.cos(2.0 * math.pi * i / 31.0)) for i in range(32)
)


def _build_fft32_tables() -> Tuple[Tuple[int, ...], Dict[int, Tuple[float, ...]], Dict[int, Tuple[float, ...]]]:
    br = tuple(int(f"{i:05b}"[::-1], 2) for i in range(32))
    tw_re: Dict[int, Tuple[float, ...]] = {}
    tw_im: Dict[int, Tuple[float, ...]] = {}
    for size in (2, 4, 8, 16, 32):
        half = size // 2
        ang = -2.0 * math.pi / size
        tw_re[size] = tuple(math.cos(ang * k) for k in range(half))
        tw_im[size] = tuple(math.sin(ang * k) for k in range(half))
    return br, tw_re, tw_im


_BR32, _TW_RE32, _TW_IM32 = _build_fft32_tables()


def _fft32_power_into(
    arr: Sequence[Any],
    b0: int,
    h: Sequence[float],
    re: List[float],
    im: List[float],
    spec: List[float],
) -> None:
    """Computes the 17 non-negative power-spectrum bins of a 32-sample windowed frame in-place."""
    br = _BR32
    for r in range(32):
        idx = br[r]
        re[r] = arr[b0 + idx] * h[idx]
        im[r] = 0.0

    for size in (2, 4, 8, 16, 32):
        half = size // 2
        wr_list = _TW_RE32[size]
        wi_list = _TW_IM32[size]
        for base in range(0, 32, size):
            for k in range(half):
                i = base + k
                j = i + half
                wr = wr_list[k]
                wi = wi_list[k]
                rj = re[j]
                ij = im[j]
                tr = wr * rj - wi * ij
                ti = wr * ij + wi * rj
                ri = re[i]
                ii = im[i]
                re[i] = ri + tr
                im[i] = ii + ti
                re[j] = ri - tr
                im[j] = ii - ti

    for k in range(17):
        rk = re[k]
        ik = im[k]
        spec[k] = rk * rk + ik * ik


def _to_stdlib_array(samples: Any) -> Tuple[array.array, float]:
    """Converts input samples into a compact stdlib `array.array` and returns `(arr, scale)`."""
    if isinstance(samples, array.array):
        scale = (1.0 / 32768.0) if samples.typecode == "h" else 1.0
        return samples, scale
    if isinstance(samples, (bytes, bytearray, memoryview)):
        arr_h = array.array("h")
        arr_h.frombytes(bytes(samples))
        return arr_h, 1.0 / 32768.0
    if hasattr(samples, "tobytes") and hasattr(samples, "dtype"):
        dt = str(samples.dtype)
        if "int16" in dt:
            arr_h = array.array("h")
            arr_h.frombytes(samples.tobytes())
            return arr_h, 1.0 / 32768.0
        if "float32" in dt:
            arr_f = array.array("f")
            arr_f.frombytes(samples.tobytes())
            return arr_f, 1.0
    return array.array("f", (float(x) for x in samples)), 1.0


def _analyze_tings_stdlib(samples: Any, sample_rate: int = 8000) -> List[float]:
    """Pure-Python stdlib kibble 'ting' detector using an in-place 32-pt Radix-2 FFT (zero numpy)."""
    if samples is None or sample_rate <= 0:
        return []
    arr, scale = _to_stdlib_array(samples)
    n_samp = len(arr)
    n_frames = (n_samp - 32) // HOP
    if n_frames <= 2:
        return []

    h = [w * scale for w in _HANN_32]
    df = float(sample_rate) / 32.0
    low_end = sum(1 for k in range(17) if k * df < 600.0)
    mid_end = sum(1 for k in range(17) if k * df < 1500.0)
    grind_end = sum(1 for k in range(17) if k * df < 2500.0)

    flux = array.array("f", [0.0] * n_frames)
    ratio = array.array("f", [0.0] * n_frames)
    re = [0.0] * 32
    im = [0.0] * 32
    spec = [0.0] * 17
    prev_log = [0.0] * 17
    has_prev = False
    _log = math.log

    for fi in range(n_frames):
        _fft32_power_into(arr, fi * HOP, h, re, im, spec)
        low = sum(spec[0:low_end])
        mid = sum(spec[low_end:mid_end])
        grind = sum(spec[mid_end:grind_end])
        high = sum(spec[grind_end:17])
        ratio[fi] = (mid + high) / (grind + low + 1e-12)

        fl = 0.0
        if has_prev:
            for k in range(low_end, 17):
                c = _log(spec[k] + 1e-10)
                p = prev_log[k]
                if c > p:
                    fl += c - p
                prev_log[k] = c
        else:
            for k in range(low_end, 17):
                prev_log[k] = _log(spec[k] + 1e-10)
            has_prev = True
        flux[fi] = fl

    sorted_flux = sorted(flux)
    med = sorted_flux[n_frames // 2]
    abs_dev = sorted(abs(x - med) for x in flux)
    mad = abs_dev[n_frames // 2]
    thr = med + ONSET_MAD_K * (mad + 1e-9)

    times: List[float] = []
    last = -MIN_SPACING_HOPS
    for i in range(1, n_frames - 1):
        fi = flux[i]
        if fi > thr and fi >= flux[i - 1] and fi >= flux[i + 1]:
            if i - last >= MIN_SPACING_HOPS and ratio[i] >= TING_SPECTRAL_RATIO_MIN:
                times.append(round(i * HOP / float(sample_rate), 4))
                last = i
    return times


def ting_times(samples: Any, sample_rate: int = 8000) -> List[float]:
    """Times (seconds) of onsets with the kibble-impact spectral signature (< 500 KB peak RAM)."""
    if samples is None or sample_rate <= 0:
        return []
    if not HAS_NUMPY or np is None or not isinstance(samples, np.ndarray):
        return _analyze_tings_stdlib(samples, sample_rate)

    n = (len(samples) - WIN) // HOP
    if n <= 2:
        return []

    scale = (1.0 / 32768.0) if samples.dtype == np.int16 else 1.0
    win = (np.hanning(WIN) * scale).astype(np.float32)
    f = np.fft.rfftfreq(WIN, 1.0 / sample_rate)
    m_low = f < 600
    m_mid = (f >= 600) & (f < 1500)
    m_grind = (f >= 1500) & (f < 2500)
    m_high = f >= 2500
    m_hb = f >= 600

    stride = samples.strides[0]
    frames = np.lib.stride_tricks.as_strided(
        samples, shape=(n, WIN), strides=(HOP * stride, stride)
    )
    ratio = np.empty(n, dtype=np.float32)
    flux = np.empty(n, dtype=np.float32)
    prev_log_hb = None
    batch = 64

    for s in range(0, n, batch):
        e = min(s + batch, n)
        spec = np.abs(np.fft.rfft(frames[s:e] * win, axis=1)).astype(np.float32)
        spec *= spec
        low = spec[:, m_low].sum(1)
        mid = spec[:, m_mid].sum(1)
        grind = spec[:, m_grind].sum(1)
        high = spec[:, m_high].sum(1)
        ratio[s:e] = (mid + high) / (grind + low + 1e-12)
        log_hb = np.log(spec[:, m_hb] + 1e-10)
        prepend = log_hb[:1] if prev_log_hb is None else prev_log_hb
        prev_log_hb = log_hb[-1:]
        diff = np.diff(log_hb, axis=0, prepend=prepend)
        np.maximum(0.0, diff, out=diff)
        flux[s:e] = diff.sum(1)

    med = float(np.median(flux))
    thr = med + ONSET_MAD_K * (float(np.median(np.abs(flux - med))) + 1e-9)
    is_peak = (flux[1:-1] > thr) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] >= flux[2:])
    times: List[float] = []
    last = -MIN_SPACING_HOPS
    for i in np.where(is_peak)[0] + 1:
        idx_i = int(i)
        if idx_i - last >= MIN_SPACING_HOPS and ratio[idx_i] >= TING_SPECTRAL_RATIO_MIN:
            times.append(round(idx_i * HOP / float(sample_rate), 4))
            last = idx_i
    return times
