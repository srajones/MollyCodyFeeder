import sys
import wave
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from watchdog.analysis import BowlAnalyzer
from watchdog.config import WatchdogConfig


def write_synthetic_wav(path: Path, samples: np.ndarray, sr: int = 16000) -> None:
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def main():
    sr = 16000
    duration = 10
    t = np.linspace(0, duration, sr * duration, endpoint=False)
    rng = np.random.default_rng(42)

    tmp_dir = Path("/tmp/poor_puppy_test")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clogged audio: background noise + 180Hz/360Hz auger motor hum (from t=2s to t=8s), NO kibble clatter
    clog_audio = rng.normal(0, 0.005, size=len(t))
    motor_window = (t >= 2.0) & (t <= 8.0)
    clog_audio[motor_window] += 0.04 * np.sin(2 * np.pi * 180 * t[motor_window]) + 0.02 * np.sin(
        2 * np.pi * 360 * t[motor_window]
    )
    clog_wav = tmp_dir / "clogged_feed.wav"
    write_synthetic_wav(clog_wav, clog_audio, sr)

    # 2. Healthy dispense audio: motor hum + 8 bursts of kibble hitting stainless steel bowl (2.5kHz - 5.5kHz)
    good_audio = clog_audio.copy()
    for burst_t in [3.1, 3.4, 3.8, 4.2, 4.7, 5.1, 5.6, 6.0]:
        hit_win = (t >= burst_t) & (t <= burst_t + 0.06)
        clink = (
            0.12 * np.sin(2 * np.pi * 3200 * t[hit_win])
            + 0.09 * np.sin(2 * np.pi * 4800 * t[hit_win])
            + rng.normal(0, 0.05, size=int(np.sum(hit_win)))
        )
        good_audio[hit_win] += clink
    good_wav = tmp_dir / "healthy_feed.wav"
    write_synthetic_wav(good_wav, good_audio, sr)

    analyzer = BowlAnalyzer(WatchdogConfig())
    res_clog = analyzer.analyze_dispense_audio(str(clog_wav))
    res_good = analyzer.analyze_dispense_audio(str(good_wav))

    print("Test 1 (Clogged Motor Only):", res_clog)
    assert res_clog.motor_detected is True and res_clog.kibble_dispensed_audio is False

    print("Test 2 (Healthy Kibble Dispense):", res_good)
    assert res_good.motor_detected is True and res_good.kibble_dispensed_audio is True

    print("✅ All acoustic clog detection tests passed!")


if __name__ == "__main__":
    main()
