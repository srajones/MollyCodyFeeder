"""Compare feature sets / models for per-window kibble detection (leave-one-clip-out)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).parent))
from eval_audio import LABELS, load_events, read_wav, segs  # noqa: E402
from train_flow import frame_feats, smooth_segments  # noqa: E402
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402

STEP = 0.25


def feats(x, sr, m0, m1):
    ff, dt = frame_feats(x, sr)
    i0, i1 = int(m0 / dt), int(m1 / dt)
    med_f = np.median(ff["flux"][i0:i1]); mad_f = np.median(np.abs(ff["flux"][i0:i1] - med_f)) + 1e-9
    onset = ff["flux"] > med_f + 4 * mad_f
    peak = np.zeros_like(onset)
    peak[1:-1] = onset[1:-1] & (ff["flux"][1:-1] >= ff["flux"][:-2]) & (ff["flux"][1:-1] >= ff["flux"][2:])
    ting = peak & (ff["ratio"] >= 1.6)
    base = {k: np.asarray(v, dtype=float) for k, v in ff.items()}
    base["ting"] = ting.astype(float); base["peak"] = peak.astype(float)
    base["lflux"] = np.log(base["flux"] + 1e-6); base["lratio"] = np.log(base["ratio"] + 1e-6)
    keys = ["low", "mid", "grind", "high", "lflux", "lratio"]
    ref80 = {k: np.percentile(base[k][i0:i1], 80) for k in keys}
    ref50 = {k: np.percentile(base[k][i0:i1], 50) for k in keys}
    rows, times = [], []
    for t in np.arange(m0, m1 - 0.25, STEP):
        r = []
        for win in (0.5, 1.0, 2.5):
            a = int(max(0, t - win / 2) / dt); b = max(a + 1, int((t + win / 2) / dt))
            r += [base["ting"][a:b].sum() / win, base["peak"][a:b].sum() / win]
            for k in keys:
                v = base[k][a:b].mean()
                r += [v - ref80[k], v - ref50[k]]
            r += [np.percentile(base["high"][a:b], 90) - np.percentile(base["high"][a:b], 50)]
        r += [(t - m0) / max(1.0, m1 - m0), m1 - t]  # position in the run
        rows.append(r); times.append(t)
    return np.array(rows), np.array(times)


if __name__ == "__main__":
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

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
        data[lab["event_id"]] = (X, y, T, res.motor_start_sec, res.motor_end_sec, lab, k)
    ids = sorted(data)
    models = {
        "logreg": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.3, class_weight="balanced", max_iter=2000)),
        "gbm": lambda: HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05, class_weight="balanced"),
    }
    for name, mk in models.items():
        accs, coverr, rows = [], [], []
        for test in ids:
            Xtr = np.vstack([data[i][0] for i in ids if i != test]); ytr = np.concatenate([data[i][1] for i in ids if i != test])
            mdl = mk().fit(Xtr, ytr)
            X, y, T, m0, m1, lab, k = data[test]
            p = mdl.predict_proba(X)[:, 1]
            sg = smooth_segments(T, p)
            cov = sum(e - s for s, e in sg) / (m1 - m0); lab_cov = sum(e - s for s, e in k) / (m1 - m0)
            accs.append(np.mean((p >= 0.5) == (y == 1))); coverr.append(abs(cov - lab_cov))
            rows.append(f"#{test}:{100*lab_cov:.0f}/{100*cov:.0f}")
        print(f"{name}: window acc {100*np.mean(accs):.1f}%  mean |coverage err| {100*np.mean(coverr):.1f} pts")
        print("   label/model coverage:", " ".join(rows))
