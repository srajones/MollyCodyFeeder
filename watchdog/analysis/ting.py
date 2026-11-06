"""Kibble "ting" detector: sharp impacts of pellets on the steel bowl / kibble pile.

Validated on the user's labeled clips (empty bowl, full bowl, multi-portion, intentional clogs, dog in the way):
real tings put their transient energy in 600-1500 Hz and >2.5 kHz, while a grinding / straining motor puts it
in 1.5-2.5 kHz and below 600 Hz. Counting spectral-flux onsets that have the ting signature separates
"food came out" (4.8-9 tings/s of motor time) from clogs (2.3-3.8/s), and still hears muffled tings when the
dog's head is in the bowl (where the old loudness-spike counter heard 3 impacts).
"""
from typing import List

import numpy as np

WIN, HOP = 256, 80  # 32 ms window, 10 ms hop at 8 kHz
TING_SPECTRAL_RATIO_MIN = 1.6  # (600-1.5k + >2.5k) / (1.5-2.5k + <600) at the onset
ONSET_MAD_K = 4.0  # onset = spectral flux above median + K * MAD
MIN_SPACING_HOPS = 3  # 30 ms between counted tings


def ting_times(samples: np.ndarray, sample_rate: int) -> List[float]:
    """Times (seconds) of onsets with the kibble-impact spectral signature."""
    n = (len(samples) - WIN) // HOP
    if n <= 2:
        return []
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(samples[idx] * np.hanning(WIN), axis=1)) ** 2
    f = np.fft.rfftfreq(WIN, 1.0 / sample_rate)
    low = spec[:, f < 600].sum(1)
    mid = spec[:, (f >= 600) & (f < 1500)].sum(1)
    grind = spec[:, (f >= 1500) & (f < 2500)].sum(1)
    high = spec[:, f >= 2500].sum(1)
    log_hb = np.log(spec[:, f >= 600] + 1e-10)
    flux = np.maximum(0.0, np.diff(log_hb, axis=0, prepend=log_hb[:1])).sum(1)
    ratio = (mid + high) / (grind + low + 1e-12)

    med = float(np.median(flux))
    thr = med + ONSET_MAD_K * (float(np.median(np.abs(flux - med))) + 1e-9)
    is_peak = (flux[1:-1] > thr) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] >= flux[2:])
    times, last = [], -MIN_SPACING_HOPS
    for i in np.where(is_peak)[0] + 1:
        if i - last >= MIN_SPACING_HOPS and ratio[i] >= TING_SPECTRAL_RATIO_MIN:
            times.append(i * HOP / sample_rate)
            last = i
    return times
