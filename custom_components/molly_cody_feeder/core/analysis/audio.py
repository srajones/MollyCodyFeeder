"""Acoustic dispense verification: auger motor hum vs. kibble impact clatter & tings.

Includes both a frugal 1D NumPy pipeline (< 1 MB peak RAM) and a pure-Python stdlib
fallback (`_analyze_audio_stdlib` using `wave`, `array`, `math`) for bare HAOS environments.
"""
from __future__ import annotations

import array
import logging
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

from custom_components.molly_cody_feeder.core.analysis.flow import (
    _analyze_flow_stdlib,
    _percentile_stdlib,
    kibble_flow,
)
from custom_components.molly_cody_feeder.core.analysis.results import (
    AudioAnalysisResult,
)
from custom_components.molly_cody_feeder.core.analysis.ting import (
    _analyze_tings_stdlib,
    ting_times,
)

HAS_NUMPY: bool = np is not None

logger = logging.getLogger("molly_cody_feeder.analysis.audio")


def _extract_wav_if_needed(media_path: str) -> Tuple[str, Optional[str]]:
    ext = Path(media_path).suffix.lower()
    if ext == ".wav":
        return media_path, None
    tmp_wav = media_path + ".extracted.wav"
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to extract audio from non-WAV files")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            media_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            tmp_wav,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    return tmp_wav, tmp_wav


def _find_motor_block(rms10: Sequence[float]) -> Tuple[int, int]:
    """Finds the longest sustained (>= 0.8s) contiguous motor activity block from 10ms RMS values."""
    n10 = len(rms10)
    if n10 == 0:
        return 0, 0
    smooth_win = 25
    half = smooth_win // 2
    act = [1 if r > 0.005 else 0 for r in rms10]
    prefix = [0] * (n10 + 1)
    s = 0
    for i, a in enumerate(act):
        s += a
        prefix[i + 1] = s

    # Centered 25-point moving average (matching np.convolve(..., ones(25)/25, mode="same"))
    # Threshold > 0.40 * 25 = 10 active 10ms frames in the 25-frame window
    idx_act: List[int] = []
    for i in range(n10):
        lo = max(0, i - half)
        hi = min(n10, i - half + smooth_win)
        if (prefix[hi] - prefix[lo]) > 10:
            idx_act.append(i)

    if not idx_act:
        return 0, 0

    blocks: List[Tuple[int, int]] = []
    b_start = idx_act[0]
    prev = idx_act[0]
    for ix in idx_act[1:]:
        if ix - prev > 120:
            blocks.append((b_start, prev))
            b_start = ix
        prev = ix
    blocks.append((b_start, prev))

    sustained = [b for b in blocks if (b[1] - b[0]) >= 80]
    if sustained:
        return max(sustained, key=lambda b: b[1] - b[0])
    return max(blocks, key=lambda b: b[1] - b[0])


def _analyze_audio_stdlib(wav_path: str, cfg: Any = None) -> AudioAnalysisResult:
    """Pure-Python stdlib acoustic anti-clog classifier using `wave`, `array`, and `math` (zero numpy)."""
    min_peaks_cfg = int(getattr(cfg, "min_kibble_audio_peaks", 4) if cfg is not None else 4)
    ting_rate_min = float(getattr(cfg, "ting_rate_min", 4.3) if cfg is not None else 4.3)

    with wave.open(wav_path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw_bytes = wf.readframes(n_frames)

    if sampwidth == 2:
        arr = array.array("h")
        arr.frombytes(raw_bytes)
        scale = 1.0 / 32768.0
    elif sampwidth == 1:
        u8 = array.array("B")
        u8.frombytes(raw_bytes)
        arr = array.array("h", ((int(b) - 128) * 256 for b in u8))
        scale = 1.0 / 32768.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")
    del raw_bytes

    if n_channels > 1:
        arr = arr[::n_channels]

    if len(arr) < sample_rate:
        return AudioAnalysisResult(
            motor_detected=False,
            kibble_impact_peaks=0,
            kibble_dispensed_audio=False,
            motor_energy_db=-100.0,
            impact_energy_db=-100.0,
            diagnosis="Audio recording too short or empty",
            motor_duration_sec=0.0,
        )

    win10 = max(1, int(sample_rate * 0.010))
    n10 = len(arr) // win10
    rms10 = array.array("f", [0.0] * n10)
    pk10 = array.array("f", [0.0] * n10)
    _sqrt = math.sqrt
    inv_win10 = 1.0 / float(win10)

    for i in range(n10):
        base = i * win10
        s2 = 0.0
        mx = 0
        for j in range(base, base + win10):
            v = arr[j]
            av = -v if v < 0 else v
            if av > mx:
                mx = av
            fv = v * scale
            s2 += fv * fv
        rms10[i] = _sqrt(s2 * inv_win10)
        pk10[i] = mx * scale

    m_start, m_end = _find_motor_block(rms10)
    motor_duration_sec = round(max(0.0, (m_end - m_start) * 0.010), 2)
    motor_detected = bool(motor_duration_sec >= 1.2)

    w_start = m_start
    w_end = min(n10, m_end + 60) if motor_detected else n10
    w_rms = rms10[w_start:w_end] if w_end > w_start else rms10
    w_pk = pk10[w_start:w_end] if w_end > w_start else pk10

    active_w_rms = [r for r in w_rms if r > 0.005]
    if len(active_w_rms) >= 10:
        motor_baseline_rms = _percentile_stdlib(active_w_rms, 45.0)
        motor_p90_rms = _percentile_stdlib(active_w_rms, 90.0)
        quiet_floor_rms = max(_percentile_stdlib(rms10, 15.0), 0.0025)
        motor_db = 20.0 * math.log10(max(motor_p90_rms, 1e-6) / quiet_floor_rms)
        impact_db = 20.0 * math.log10(max(max(w_rms) if w_rms else 1e-6, 1e-6) / max(motor_baseline_rms, 0.005))
    else:
        motor_baseline_rms = 0.01
        motor_db = 0.0
        impact_db = 0.0

    rms_spike_thresh = max(0.165, min(0.20, motor_baseline_rms * 1.55))
    peaks = sum(1 for r, p in zip(w_rms, w_pk) if r > rms_spike_thresh and p > 0.40)

    min_peaks_needed = max(15, min_peaks_cfg)
    win_t0, win_t1 = w_start * 0.010, w_end * 0.010
    tings = (
        [t for t in _analyze_tings_stdlib(arr, sample_rate) if win_t0 <= t <= win_t1]
        if motor_detected
        else []
    )
    window_sec = max(0.0, win_t1 - win_t0)
    ting_rate = len(tings) / window_sec if motor_detected and window_sec > 0 else 0.0
    kibble_dispensed = bool(
        motor_detected
        and (
            ting_rate >= ting_rate_min
            or (peaks >= min_peaks_needed and ting_rate >= ting_rate_min * 0.7)
        )
    )

    if kibble_dispensed:
        diagnosis = (
            f"Motor churned ({motor_duration_sec:.1f}s) AND kibble heard hitting the bowl "
            f"({len(tings)} tings, {ting_rate:.1f}/s; {peaks} loud spikes)."
        )
    elif motor_detected and not kibble_dispensed:
        diagnosis = (
            f"CLOG / EMPTY HOPPER SOUND DETECTED: Motor churned for {motor_duration_sec:.1f}s "
            f"(+{motor_db:.1f} dB) without the kibble 'ting' ({ting_rate:.1f} tings/s < {ting_rate_min:.1f})."
        )
    else:
        diagnosis = "No sustained auger motor hum or kibble impacts detected on audio."

    res = AudioAnalysisResult(
        motor_detected=motor_detected,
        kibble_impact_peaks=peaks,
        kibble_dispensed_audio=kibble_dispensed,
        motor_energy_db=round(float(motor_db), 2),
        impact_energy_db=round(float(impact_db), 2),
        diagnosis=diagnosis,
        motor_duration_sec=motor_duration_sec,
        kibble_rate_per_sec=round(peaks / motor_duration_sec, 2) if motor_duration_sec > 0 else 0.0,
        ting_count=len(tings),
        ting_rate_per_sec=round(ting_rate, 2),
    )
    if motor_detected:
        res.motor_start_sec = round(m_start * 0.010, 2)
        res.motor_end_sec = round(m_end * 0.010, 2)
        flow = _analyze_flow_stdlib(arr, sample_rate, res.motor_start_sec, res.motor_end_sec)
        if flow is not None:
            res.kibble_coverage = flow.coverage
            res.flow_coverage = flow.coverage
            res.longest_gap_sec = flow.longest_gap_sec
            res.tail_gap_sec = flow.tail_gap_sec
            res.kibble_gaps = [list(g) for g in flow.gaps]
            res.flow_segments = list(flow.segments)
    return res


class AudioMixin:
    """Acoustic dispense analyzer: motor window isolation + kibble ting & clatter detection."""

    def analyze_dispense_audio(self, media_path: str) -> AudioAnalysisResult:
        wav_path, tmp_wav = _extract_wav_if_needed(media_path)
        try:
            if not HAS_NUMPY or np is None:
                return _analyze_audio_stdlib(wav_path, getattr(self, "cfg", None))

            with wave.open(wav_path, "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                raw_bytes = wf.readframes(n_frames)

            if sampwidth == 2:
                pcm16 = np.frombuffer(raw_bytes, dtype=np.int16)
                if n_channels > 1:
                    pcm16 = pcm16.reshape(-1, n_channels)[:, 0]
            elif sampwidth == 1:
                u8 = np.frombuffer(raw_bytes, dtype=np.uint8)
                if n_channels > 1:
                    u8 = u8.reshape(-1, n_channels)[:, 0]
                pcm16 = ((u8.astype(np.int16) - 128) << 8)
            else:
                raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

            if len(pcm16) < sample_rate:
                return AudioAnalysisResult(
                    motor_detected=False,
                    kibble_impact_peaks=0,
                    kibble_dispensed_audio=False,
                    motor_energy_db=-100.0,
                    impact_energy_db=-100.0,
                    diagnosis="Audio recording too short or empty",
                    motor_duration_sec=0.0,
                )

            # 1. Analyze 10ms frames in small batches directly from int16 (< 50 KB allocation)
            win10 = max(1, int(sample_rate * 0.010))
            n10 = len(pcm16) // win10
            frames10 = pcm16[: n10 * win10].reshape(n10, win10)
            rms10 = np.empty(n10, dtype=np.float32)
            pk10 = np.empty(n10, dtype=np.float32)
            scale = np.float32(1.0 / 32768.0)
            batch10 = 256
            for s in range(0, n10, batch10):
                e = min(s + batch10, n10)
                f_chunk = frames10[s:e].astype(np.float32)
                f_chunk *= scale
                pk10[s:e] = np.max(np.abs(f_chunk), axis=1)
                f_chunk *= f_chunk
                rms10[s:e] = np.sqrt(np.mean(f_chunk, axis=1))

            m_start, m_end = _find_motor_block(rms10)
            motor_duration_sec = round(max(0.0, (m_end - m_start) * 0.010), 2)
            motor_detected = bool(motor_duration_sec >= 1.2)

            w_start = m_start
            w_end = min(n10, m_end + 60) if motor_detected else n10
            w_rms = rms10[w_start:w_end] if w_end > w_start else rms10
            w_pk = pk10[w_start:w_end] if w_end > w_start else pk10

            active_w_rms = w_rms[w_rms > 0.005]
            if len(active_w_rms) >= 10:
                motor_baseline_rms = float(np.percentile(active_w_rms, 45))
                motor_p90_rms = float(np.percentile(active_w_rms, 90))
                quiet_floor_rms = max(float(np.percentile(rms10, 15)), 0.0025)
                motor_db = 20.0 * math.log10(max(motor_p90_rms, 1e-6) / quiet_floor_rms)
                impact_db = 20.0 * math.log10(max(float(np.max(w_rms)), 1e-6) / max(motor_baseline_rms, 0.005))
            else:
                motor_baseline_rms = 0.01
                motor_db = 0.0
                impact_db = 0.0

            rms_spike_thresh = max(0.165, min(0.20, motor_baseline_rms * 1.55))
            spike_mask = (w_rms > rms_spike_thresh) & (w_pk > 0.40)
            peaks = int(np.sum(spike_mask))

            cfg = getattr(self, "cfg", None)
            min_peaks_cfg = int(getattr(cfg, "min_kibble_audio_peaks", 4) if cfg is not None else 4)
            ting_rate_min = float(getattr(cfg, "ting_rate_min", 4.3) if cfg is not None else 4.3)

            min_peaks_needed = max(15, min_peaks_cfg)
            win_t0, win_t1 = w_start * 0.010, w_end * 0.010
            tings = [t for t in ting_times(pcm16, sample_rate) if win_t0 <= t <= win_t1] if motor_detected else []
            window_sec = max(0.0, win_t1 - win_t0)
            ting_rate = len(tings) / window_sec if motor_detected and window_sec > 0 else 0.0
            kibble_dispensed = bool(
                motor_detected
                and (
                    ting_rate >= ting_rate_min
                    or (peaks >= min_peaks_needed and ting_rate >= ting_rate_min * 0.7)
                )
            )

            if kibble_dispensed:
                diagnosis = (
                    f"Motor churned ({motor_duration_sec:.1f}s) AND kibble heard hitting the bowl "
                    f"({len(tings)} tings, {ting_rate:.1f}/s; {peaks} loud spikes)."
                )
            elif motor_detected and not kibble_dispensed:
                diagnosis = (
                    f"CLOG / EMPTY HOPPER SOUND DETECTED: Motor churned for {motor_duration_sec:.1f}s "
                    f"(+{motor_db:.1f} dB) without the kibble 'ting' ({ting_rate:.1f} tings/s < {ting_rate_min:.1f})."
                )
            else:
                diagnosis = "No sustained auger motor hum or kibble impacts detected on audio."

            res = AudioAnalysisResult(
                motor_detected=motor_detected,
                kibble_impact_peaks=peaks,
                kibble_dispensed_audio=kibble_dispensed,
                motor_energy_db=round(float(motor_db), 2),
                impact_energy_db=round(float(impact_db), 2),
                diagnosis=diagnosis,
                motor_duration_sec=motor_duration_sec,
                kibble_rate_per_sec=round(peaks / motor_duration_sec, 2) if motor_duration_sec > 0 else 0.0,
                ting_count=len(tings),
                ting_rate_per_sec=round(ting_rate, 2),
            )
            if motor_detected:
                res.motor_start_sec = round(m_start * 0.010, 2)
                res.motor_end_sec = round(m_end * 0.010, 2)
                flow = kibble_flow(pcm16, sample_rate, res.motor_start_sec, res.motor_end_sec)
                if flow is not None:
                    res.kibble_coverage = flow.coverage
                    res.flow_coverage = flow.coverage
                    res.longest_gap_sec = flow.longest_gap_sec
                    res.tail_gap_sec = flow.tail_gap_sec
                    res.kibble_gaps = [list(g) for g in flow.gaps]
                    res.flow_segments = list(flow.segments)
            return res
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except OSError:
                    pass


AudioAnalysisMixin = AudioMixin
