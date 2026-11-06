"""Longest stretch of the motor run with (almost) no kibble 'tings' -> jam signal. Uses production ting_times."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).parent))
from eval_audio import LABELS, load_events, read_wav, segs  # noqa: E402
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.analysis.ting import ting_times  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402


def longest_low(tings, m0, m1, win, thr, step=0.1):
    tings = np.asarray(tings)
    best = cur = 0.0
    for t in np.arange(m0 + win / 2, m1 - win / 2, step):
        rate = np.sum((tings >= t - win / 2) & (tings < t + win / 2)) / win
        cur = cur + step if rate < thr else 0.0
        best = max(best, cur)
    return best + (win if best > 0 else 0)  # the window itself spans the gap edges


an = BowlAnalyzer(WatchdogConfig())
ev = load_events()
rows = []
for key, lab in LABELS.items():
    m = segs(lab, "motor")
    if not m:
        continue
    row, d, wav = ev[key]
    x, sr = read_wav(wav)
    res = an.analyze_dispense_audio(str(wav))
    tt = ting_times(x, sr)
    rows.append((lab["event_id"], lab.get("problem"), res, tt))
configs = [(w, t) for w in (2.0, 3.0, 4.0) for t in (1.0, 1.5, 2.0, 2.5, 3.0)]
print("config        jam gaps (s)                      | worst normal (s) | cant_tell")
for w, thr in configs:
    jam = sorted(longest_low(tt, r.motor_start_sec, r.motor_end_sec, w, thr) for i, p, r, tt in rows if p == "jam")
    norm = sorted(((longest_low(tt, r.motor_start_sec, r.motor_end_sec, w, thr), i) for i, p, r, tt in rows if p == "none"), reverse=True)
    ct = [round(longest_low(tt, r.motor_start_sec, r.motor_end_sec, w, thr), 1) for i, p, r, tt in rows if p == "cant_tell"]
    print(f"win={w} thr={thr}: {[round(v, 1) for v in jam]} | {[(round(v, 1), i) for v, i in norm[:3]]} | {ct}")
