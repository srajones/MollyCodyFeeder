"""Evaluate the production audio analyzer against the hand-checked labels (offline, read-only)."""
import json
import sqlite3
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from watchdog.analysis import BowlAnalyzer  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402
from watchdog.analysis.ting import ting_times  # noqa: E402

B = sorted((ROOT / "exports").glob("poor-puppy-ml-*/"))[-1]
LABELS = json.loads((ROOT / "ml/labels/feed_event_labels.json").read_text())


def load_events():
    c = sqlite3.connect(B / "data/kibble_history.db")
    c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("SELECT * FROM feed_events"):
        d = json.loads(r["details_json"] or "{}")
        wav = B / "captures" / Path(d.get("audio_clip") or r["audio_clip"] or "x").name
        out[f"{r['timestamp']}|{r['id']}"] = (dict(r), d, wav)
    return out


def read_wav(p):
    with wave.open(str(p), "rb") as wf:
        sr = wf.getframerate()
        x = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return x, sr


def segs(lab, typ):
    return sorted((s["start"], s["end"]) for s in lab.get("segments", []) if s["type"] == typ)


def total(iv):
    return sum(e - s for s, e in iv)


if __name__ == "__main__":
    an = BowlAnalyzer(WatchdogConfig())
    ev = load_events()
    print(f"{'#':>3} {'label':<8} {'prob':<10} {'lab motor':>9} {'motor':>11} {'lab kib%':>8} {'det kib%':>8} "
          f"{'frameAgree':>10} {'longGap':>7} {'tail':>5} {'tings/s':>7}")
    for key, lab in sorted(LABELS.items(), key=lambda kv: kv[1]["event_id"]):
        row, d, wav = ev[key]
        if not wav.exists():
            continue
        res = an.analyze_dispense_audio(str(wav))
        m, k = segs(lab, "motor"), segs(lab, "kibble")
        lm, lk = total(m), total(k)
        agree = "-"
        if m and res.kibble_coverage is not None:
            from watchdog.analysis.flow import kibble_flow
            x, sr = read_wav(wav)
            fl = kibble_flow(x, sr, res.motor_start_sec, res.motor_end_sec)
            ts = np.arange(m[0][0], m[-1][1], 0.05)
            inside = lambda iv, t: any(s <= t <= e for s, e in iv)  # noqa: E731
            agree = f"{100 * np.mean([inside(k, t) == inside(fl.segments, t) for t in ts]):.0f}%"
        mot = f"{res.motor_start_sec}-{res.motor_end_sec}" if res.motor_start_sec is not None else "-"
        print(f"{lab['event_id']:>3} {str(lab.get('food_dispensed')):<8} {str(lab.get('problem')):<10} "
              f"{lm:9.1f} {mot:>11} {(100 * lk / lm if lm else 0):7.0f}% "
              f"{(100 * res.kibble_coverage if res.kibble_coverage is not None else 0):7.0f}% {agree:>10} "
              f"{res.longest_gap_sec or 0:7.1f} {res.tail_gap_sec or 0:5.1f} {res.ting_rate_per_sec:7.2f}")
