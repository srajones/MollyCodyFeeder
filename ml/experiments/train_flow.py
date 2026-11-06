"""Train/evaluate a per-window 'kibble falling?' classifier on the hand-checked segments.

Leave-one-clip-out logistic regression (numpy only, so the result can ship to the VPS as a few weights).
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).parent))
from eval_audio import LABELS, load_events, read_wav, segs  # noqa: E402
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402

WIN_SEC, STEP_SEC = 1.0, 0.25
N, HOP = 256, 80  # 10 ms hop


def frame_feats(x, sr):
    n = (len(x) - N) // HOP
    idx = np.arange(N)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(N), axis=1)) ** 2
    f = np.fft.rfftfreq(N, 1.0 / sr)
    band = lambda a, b: spec[:, (f >= a) & (f < b)].sum(1) + 1e-12  # noqa: E731
    low, mid, grind, high = band(0, 600), band(600, 1500), band(1500, 2500), band(2500, 4001)
    log_hb = np.log(spec[:, f >= 600] + 1e-10)
    flux = np.maximum(0.0, np.diff(log_hb, axis=0, prepend=log_hb[:1])).sum(1)
    ratio = (mid + high) / (grind + low)
    return dict(low=10 * np.log10(low), mid=10 * np.log10(mid), grind=10 * np.log10(grind),
                high=10 * np.log10(high), flux=flux, ratio=ratio), HOP / sr


def window_matrix(x, sr, m0, m1):
    ff, dt = frame_feats(x, sr)
    i0, i1 = int(m0 / dt), int(m1 / dt)
    typ = {k: np.median(v[i0:i1]) for k, v in ff.items()}
    med_f = np.median(ff["flux"][i0:i1]); mad_f = np.median(np.abs(ff["flux"][i0:i1] - med_f)) + 1e-9
    onset = (ff["flux"] > med_f + 4 * mad_f)
    peak = np.zeros_like(onset); peak[1:-1] = onset[1:-1] & (ff["flux"][1:-1] >= ff["flux"][:-2]) & (ff["flux"][1:-1] >= ff["flux"][2:])
    ting = peak & (ff["ratio"] >= 1.6)
    rows, times = [], []
    w = int(WIN_SEC / dt)
    for t in np.arange(m0, m1 - WIN_SEC / 2, STEP_SEC):
        a = int(max(0, t - WIN_SEC / 2) / dt); b = a + w
        sl = slice(a, b)
        rows.append([
            ting[sl].sum() / WIN_SEC,                         # ting rate
            peak[sl].sum() / WIN_SEC,                         # all onsets
            np.mean(ff["mid"][sl]) - typ["mid"],
            np.mean(ff["high"][sl]) - typ["high"],
            np.mean(ff["grind"][sl]) - typ["grind"],
            np.mean(ff["low"][sl]) - typ["low"],
            np.log(np.mean(ff["ratio"][sl]) + 1e-6) - np.log(typ["ratio"] + 1e-6),
            np.log(np.mean(ff["flux"][sl]) + 1e-6) - np.log(typ["flux"] + 1e-6),
            np.percentile(ff["high"][sl], 90) - np.percentile(ff["high"][sl], 50),  # high-band spikiness
        ])
        times.append(t)
    return np.array(rows), np.array(times)


FEATS = ["ting_rate", "onsets", "mid", "high", "grind", "low", "ratio", "flux", "hi_spiky"]


def fit_logreg(X, y, l2=1.0, iters=3000, lr=0.1):
    w = np.zeros(X.shape[1]); b = 0.0
    pos = y.mean(); cw = np.where(y == 1, 0.5 / pos, 0.5 / (1 - pos))  # balanced
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = (p - y) * cw
        w -= lr * (X.T @ g / len(y) + l2 * w / len(y))
        b -= lr * g.mean()
    return w, b


def smooth_segments(times, prob, thr=0.5, merge=0.8, min_len=0.5):
    on = prob >= thr
    segs_, start = [], None
    for t, v in zip(times, on):
        if v and start is None:
            start = t
        if not v and start is not None:
            segs_.append([start, t]); start = None
    if start is not None:
        segs_.append([start, times[-1] + STEP_SEC])
    merged = []
    for s in segs_:
        if merged and s[0] - merged[-1][1] <= merge:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [s for s in merged if s[1] - s[0] >= min_len]


if __name__ == "__main__":
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
        m0, m1 = res.motor_start_sec, res.motor_end_sec
        X, T = window_matrix(x, sr, m0, m1)
        y = np.array([any(s <= t <= e for s, e in k) for t in T], dtype=float)
        data[lab["event_id"]] = (X, y, T, m0, m1, lab, k)
    ids = sorted(data)
    allX = np.vstack([data[i][0] for i in ids])
    mu, sd = allX.mean(0), allX.std(0) + 1e-9
    print(f"{len(ids)} clips, {len(allX)} windows, kibble share {np.mean(np.concatenate([data[i][1] for i in ids])):.2f}")
    tot_acc, rows = [], []
    for test in ids:
        Xtr = np.vstack([(data[i][0] - mu) / sd for i in ids if i != test])
        ytr = np.concatenate([data[i][1] for i in ids if i != test])
        w, b = fit_logreg(Xtr, ytr)
        X, y, T, m0, m1, lab, k = data[test]
        p = 1 / (1 + np.exp(-(((X - mu) / sd) @ w + b)))
        segs_ = smooth_segments(T, p)
        cov = sum(e - s for s, e in segs_) / (m1 - m0)
        lab_cov = sum(e - s for s, e in k) / (m1 - m0)
        acc = np.mean((p >= 0.5) == (y == 1))
        tot_acc.append(acc)
        rows.append((test, lab.get("food_dispensed"), lab.get("problem"), lab_cov, cov, acc))
    print(f"{'#':>3} {'food':<8} {'problem':<10} {'lab kib%':>8} {'model kib%':>10} {'win acc':>7}")
    for r in rows:
        print(f"{r[0]:>3} {str(r[1]):<8} {str(r[2]):<10} {100*r[3]:7.0f}% {100*r[4]:9.0f}% {100*r[5]:6.0f}%")
    print(f"mean held-out window accuracy {100*np.mean(tot_acc):.1f}%")
    w, b = fit_logreg(np.vstack([(data[i][0] - mu) / sd for i in ids]), np.concatenate([data[i][1] for i in ids]))
    print("weights:", dict(zip(FEATS, np.round(w, 2))), "bias", round(b, 2))
    json.dump({"feats": FEATS, "mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": float(b),
               "win_sec": WIN_SEC, "step_sec": STEP_SEC},
              open(Path(__file__).parent / "kibble_model.json", "w"), indent=1)
