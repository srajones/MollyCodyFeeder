"""Owner-labelled feed regression (v0.6): replays every hand-checked feed in ml/labels/ through the real
audio analyzer + decision rules and checks them against the owner's ground truth.

Needs the exported bundle (git-ignored) under exports/poor-puppy-ml-*/; skipped automatically otherwise.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdog.analysis import BowlAnalyzer, assess_dispense  # noqa: E402
from watchdog.config import WatchdogConfig  # noqa: E402

LABELS = ROOT / "ml" / "labels" / "feed_event_labels.json"
BUNDLES = sorted(p for p in (ROOT / "exports").glob("poor-puppy-ml-*") if p.is_dir()) if (ROOT / "exports").is_dir() else []


def _cases():
    if not BUNDLES or not LABELS.is_file():
        return []
    bundle = BUNDLES[-1]
    dbs = list((bundle / "data").glob("*.db"))
    if not dbs:
        return []
    con = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = {f"{r['timestamp']}|{r['id']}": r for r in con.execute("SELECT * FROM feed_events")}
    out = []
    for key, lab in json.loads(LABELS.read_text()).items():
        r = rows.get(key)
        if r is None:
            continue
        d = json.loads(r["details_json"] or "{}")
        wav = bundle / "captures" / Path(d.get("audio_clip") or r["audio_clip"] or "missing").name
        if wav.is_file():
            out.append((lab, r, wav))
    return out


CASES = _cases()


@unittest.skipUnless(CASES, "owner-labelled bundle not present (exports/ is git-ignored)")
class OwnerLabelledFeeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.an = BowlAnalyzer(WatchdogConfig())
        cls.results = [(lab, r, cls.an.analyze_dispense_audio(str(wav))) for lab, r, wav in CASES]

    def test_fed_vs_not_fed_matches_owner(self):
        for lab, r, res in self.results:
            truth = lab.get("food_dispensed")
            if truth == "yes":
                self.assertTrue(res.kibble_dispensed_audio, f"#{lab['event_id']}: owner heard food, analyzer did not")
            elif truth == "no":
                self.assertFalse(res.kibble_dispensed_audio, f"#{lab['event_id']}: owner heard no food")

    def test_motor_run_matches_owner_segments(self):
        import wave
        for (lab, r, res), (_, _, wav) in zip(self.results, CASES):
            motor = [s for s in lab.get("segments", []) if s["type"] == "motor"]
            if not motor or res.motor_start_sec is None:
                continue
            with wave.open(str(wav)) as wf:
                wav_sec = wf.getnframes() / wf.getframerate()
            self.assertLess(abs(res.motor_start_sec - motor[0]["start"]), 1.0, f"#{lab['event_id']} motor start")
            if wav_sec - res.motor_end_sec > 2.0:  # recording ended while the motor ran: end unknown
                # jams can end with a few seconds of strained/stuttering motor that's hard to mark by ear
                tol = 2.5 if lab.get("problem") == "jam" else 1.0
                self.assertLess(abs(res.motor_end_sec - motor[-1]["end"]), tol, f"#{lab['event_id']} motor end")

    def test_every_owner_jam_gets_a_weak_flow_or_short_run_warning(self):
        cfg = WatchdogConfig()
        for lab, r, res in self.results:
            if lab.get("problem") != "jam" or not res.kibble_dispensed_audio:
                continue
            f = assess_dispense(res, int(r["portions"] or 0), weak_flow_ting_rate=cfg.weak_flow_ting_rate)
            self.assertTrue(f["weak_flow"] or f["short_run"], f"#{lab['event_id']}: owner jam not flagged ({f})")

    def test_weak_flow_false_alarms_stay_rare(self):
        cfg = WatchdogConfig()
        normal = [(lab, r, res) for lab, r, res in self.results if lab.get("problem") == "none" and res.kibble_dispensed_audio]
        flagged = [lab["event_id"] for lab, r, res in normal
                   if assess_dispense(res, int(r["portions"] or 0), weak_flow_ting_rate=cfg.weak_flow_ting_rate)["weak_flow"]]
        self.assertLessEqual(len(flagged), max(1, len(normal) // 5), f"too many false weak-flow warnings: {flagged}")


if __name__ == "__main__":
    unittest.main()
