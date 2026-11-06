"""Calibrate the clog classifier from your Review Lab labels (numpy only, runs in seconds on a 2-vCPU VPS).

    .venv/bin/python scripts/calibrate.py            # report + train when enough labels exist
    .venv/bin/python scripts/calibrate.py --min-labels 6

What it does
1. Re-analyzes every user-reviewed clip in captures/review_clips/ (training_labels.json).
2. Prints classifier-vs-label agreement and the feature margins that separate "food came out" from
   "motor ran but nothing came out" (impact spikes, impacts/second).
3. With enough labels (both classes present) it fits a tiny L2 logistic regression on 4 audio features and
   writes data/audio_model.json. The watchdog then reports `ml_prob_dispensed` next to the rule-based verdict
   and flags disagreements for review. The rule-based verdict stays authoritative.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdog.analysis import AUDIO_MODEL_FEATURES, BowlAnalyzer, audio_features  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402

CLIPS = ROOT / "captures" / "review_clips"
MODEL_PATH = ROOT / "data" / "audio_model.json"


def fit_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 4000, lr: float = 0.1):
    w = np.zeros(X.shape[1])
    b = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(X @ w + b)))
        g = p - y
        w -= lr * (X.T @ g / len(y) + l2 * w / len(y))
        b -= lr * float(np.mean(g))
    return w, b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-labels", type=int, default=8)
    args = ap.parse_args()

    labels_file = CLIPS / "training_labels.json"
    if not labels_file.exists():
        print("No training_labels.json yet — label clips in the Review Lab first.")
        return 1
    analyzer = BowlAnalyzer(WatchdogConfig())
    rows, ys = [], []
    print(f"{'clip':28s} {'label':6s} {'rules':6s} {'motor_s':>7s} {'peaks':>5s} {'ting/s':>6s}")
    for rec in json.loads(labels_file.read_text()):
        wav = CLIPS / f"{rec['clip_id']}.wav"
        if not rec.get("user_reviewed") or not wav.exists():
            continue
        if bool(rec.get("feed_success")) == bool(rec.get("is_clog")):
            continue  # outcome not marked (neither or both boxes): not usable as a label
        a = analyzer.analyze_dispense_audio(str(wav))
        if not a.motor_detected:
            continue  # only "motor ran" clips say anything about clog vs. dispense
        y = 1.0 if rec.get("feed_success") else 0.0
        rows.append(audio_features(a))
        ys.append(y)
        flag = "" if bool(y) == a.kibble_dispensed_audio else "  <-- DISAGREES"
        print(f"{rec['clip_id']:28s} {'food' if y else 'CLOG':6s} {'food' if a.kibble_dispensed_audio else 'CLOG':6s} "
              f"{a.motor_duration_sec:7.1f} {a.kibble_impact_peaks:5d} {a.ting_rate_per_sec:6.1f}{flag}")

    if not rows:
        print("No reviewed clips with motor sound found.")
        return 1
    X = np.array(rows, dtype=float)
    y = np.array(ys)
    pos, neg = X[y == 1], X[y == 0]
    rate_i = AUDIO_MODEL_FEATURES.index("ting_rate")
    if len(pos) and len(neg):
        lo, hi = float(neg[:, rate_i].max()), float(pos[:, rate_i].min())
        verdict = "separable" if hi > lo else "OVERLAPPING (more labels needed)"
        print(f"\nkibble tings/second: clogs <= {lo:.1f}, good feeds >= {hi:.1f} -> {verdict} (TING_RATE_MIN between them)")
    print(f"labels: {int(y.sum())} food / {int(len(y) - y.sum())} clog")

    if len(y) < args.min_labels or len(pos) < 2 or len(neg) < 2:
        print(f"Not training yet: need >= {args.min_labels} labeled motor clips with >= 2 of each class.")
        return 0

    mean, std = X.mean(0), X.std(0) + 1e-6
    Xs = (X - mean) / std
    # leave-one-out accuracy so you know whether the model can be trusted
    hits = 0
    for i in range(len(y)):
        m = np.ones(len(y), bool)
        m[i] = False
        w, b = fit_logreg(Xs[m], y[m])
        hits += int((1 / (1 + math.exp(-(Xs[i] @ w + b))) >= 0.5) == bool(y[i]))
    w, b = fit_logreg(Xs, y)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(json.dumps({
        "features": AUDIO_MODEL_FEATURES, "mean": mean.tolist(), "std": std.tolist(),
        "w": w.tolist(), "b": b, "n": int(len(y)), "loo_accuracy": hits / len(y),
    }, indent=2))
    print(f"Saved {MODEL_PATH} (leave-one-out accuracy {hits}/{len(y)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
