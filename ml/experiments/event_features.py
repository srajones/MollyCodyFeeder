"""Event-level: which features separate the owner's 'jam/partial' feeds from normal ones?"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).parent))
from compare_models import feats  # noqa: E402
from eval_audio import LABELS, load_events, read_wav, segs  # noqa: E402
from train_flow import smooth_segments  # noqa: E402
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402


def longest_gap(sg, m0, m1):
    pts = [m0] + [v for s in sg for v in s] + [m1]
    return max(pts[i + 1] - pts[i] for i in range(0, len(pts) - 1, 2))


an = BowlAnalyzer(WatchdogConfig())
ev = load_events()
data = {}
for key, lab in LABELS.items():
    m, k = segs(lab, "motor"), segs(lab, "kibble")
    if not m:
        continue
    row, d, wav = ev[key]
    x, sr = read_wav(wav)
    res = an.analyze_dispense_audio(str(wav))
    X, T = feats(x, sr, res.motor_start_sec, res.motor_end_sec)
    y = np.array([any(s <= t <= e for s, e in k) for t in T], dtype=int)
    data[lab["event_id"]] = (X, y, T, res, lab, k, row)
ids = sorted(data)
print(f"{'#':>3} {'problem':<10} {'food':<8} {'tings/s':>7} {'motor/exp':>9} {'labGap':>6} {'gbmGap':>6} {'gbmCov':>6} {'2ndHalf/1st tings':>17}")
for test in ids:
    Xtr = np.vstack([data[i][0] for i in ids if i != test]); ytr = np.concatenate([data[i][1] for i in ids if i != test])
    mdl = HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05, class_weight="balanced").fit(Xtr, ytr)
    X, y, T, res, lab, k, row = data[test]
    m0, m1 = res.motor_start_sec, res.motor_end_sec
    sg = smooth_segments(T, mdl.predict_proba(X)[:, 1])
    cov = sum(e - s for s, e in sg) / (m1 - m0)
    exp = max(1, row["portions"]) * 2.35
    tr = X[:, 0]  # ting rate (0.5 s window)
    h = len(tr) // 2
    print(f"{test:>3} {str(lab.get('problem')):<10} {str(lab.get('food_dispensed')):<8} {res.ting_rate_per_sec:7.2f} "
          f"{(m1 - m0) / exp:9.2f} {longest_gap(k, m0, m1):6.1f} {longest_gap(sg, m0, m1):6.1f} {100 * cov:5.0f}% "
          f"{(tr[h:].mean() + 1e-6) / (tr[:h].mean() + 1e-6):17.2f}")
