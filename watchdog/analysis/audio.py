"""Acoustic clog detection: auger motor window vs. kibble impact clatter."""

import wave

import numpy as np

from watchdog.analysis.flow import kibble_flow
from watchdog.analysis.learning import _model_prob
from watchdog.analysis.results import AudioAnalysisResult
from watchdog.analysis.ting import ting_times


class AudioAnalysisMixin:
    """Motor-hum vs. kibble-impact analysis of a dispense recording."""

    # -------------------------------------------------------------------------
    # Acoustic Analysis (Motor Hum vs. Kibble Impact Clatter on Bowl)
    # -------------------------------------------------------------------------
    def analyze_dispense_audio(self, wav_path: str) -> AudioAnalysisResult:
        """
        Analyzes a recorded WAV file during a feed window (supports both 8000 Hz Cloud `imm`
        G.711 A-law recordings and 16000 Hz recordings):
        - First isolates the contiguous auger motor churning window(s) (which scale from ~2.6s
          for 1 portion up to ~45s+ for 17-20 portions) and measures `motor_duration_sec`.
        - Inside the active motor window (+ 0.6s tail for falling pellets), separates the steady
          motor hum baseline from the sharp percussive clatter spikes of kibble hitting the metal
          dish or existing kibble pile.
        - If the motor churns normally (`motor_detected=True`) without kibble impact spikes above
          the motor baseline (`kibble_dispensed_audio=False`), flags a CLOG / EMPTY HOPPER.
        """
        with wave.open(wav_path, "rb") as wf:
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw_bytes = wf.readframes(n_frames)

        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < sample_rate:
            return AudioAnalysisResult(
                motor_detected=False,
                kibble_impact_peaks=0,
                kibble_dispensed_audio=False,
                motor_energy_db=-100.0,
                impact_energy_db=-100.0,
                diagnosis="Audio recording too short or empty",
                motor_duration_sec=0.0,
            )

        # 1. Analyze 10ms frames to isolate the auger motor rotation window and transient clatter spikes
        win10 = max(1, int(sample_rate * 0.010))
        n10 = len(samples) // win10
        frames10 = samples[: n10 * win10].reshape(n10, win10)
        rms10 = np.sqrt(np.mean(frames10 ** 2, axis=1))
        pk10 = np.max(np.abs(frames10), axis=1)

        # Smooth acoustic gate over 250ms (25 x 10ms) to locate contiguous motor rotation blocks
        smooth_win = 25
        act_smooth = np.convolve(
            (rms10 > 0.005).astype(np.float32),
            np.ones(smooth_win, dtype=np.float32) / float(smooth_win),
            mode="same",
        )
        idx_act = np.where(act_smooth > 0.40)[0]

        blocks = []
        if len(idx_act) > 0:
            b_start = int(idx_act[0])
            prev = int(idx_act[0])
            for ix in idx_act[1:]:
                ix_int = int(ix)
                # Separate distinct bursts if there is >1.2s of silence between them
                if ix_int - prev > 120:
                    blocks.append((b_start, prev))
                    b_start = ix_int
                prev = ix_int
            blocks.append((b_start, prev))

        # Select the primary dispense motor block: the LONGEST sustained (>= 0.8s) block. Scheduled
        # checks record ~70 s around the meal, so a bark / voice burst can come before the motor;
        # taking the first block would misread it as "motor ran, no kibble" (false clog alert).
        sustained_blocks = [b for b in blocks if (b[1] - b[0]) >= 80]
        if sustained_blocks:
            m_start, m_end = max(sustained_blocks, key=lambda b: b[1] - b[0])
        elif blocks:
            m_start, m_end = max(blocks, key=lambda b: b[1] - b[0])
        else:
            m_start, m_end = 0, 0

        motor_duration_sec = round(max(0.0, (m_end - m_start) * 0.010), 2)
        motor_detected = bool(motor_duration_sec >= 1.2)

        # Extend analysis window by +0.60s after motor stops to capture the last falling pellets
        w_start = m_start
        w_end = min(n10, m_end + 60) if motor_detected else n10
        w_rms = rms10[w_start:w_end] if w_end > w_start else rms10
        w_pk = pk10[w_start:w_end] if w_end > w_start else pk10

        active_w_rms = w_rms[w_rms > 0.005]
        if len(active_w_rms) >= 10:
            motor_baseline_rms = float(np.percentile(active_w_rms, 45))
            motor_p90_rms = float(np.percentile(active_w_rms, 90))
            quiet_floor_rms = max(float(np.percentile(rms10, 15)), 0.0025)
            motor_db = 20.0 * np.log10(max(motor_p90_rms, 1e-6) / quiet_floor_rms)
            impact_db = 20.0 * np.log10(max(float(np.max(w_rms)), 1e-6) / max(motor_baseline_rms, 0.005))
        else:
            motor_baseline_rms = 0.01
            motor_db = 0.0
            impact_db = 0.0

        # Kibble pellets hitting the metal dish or kibble pile produce sharp 10ms spikes well above
        # the steady auger motor hum (rms > 0.165 and peak > 0.40)
        rms_spike_thresh = max(0.165, min(0.20, motor_baseline_rms * 1.55))
        spike_mask = (w_rms > rms_spike_thresh) & (w_pk > 0.40)
        peaks = int(np.sum(spike_mask))

        # Primary evidence: the kibble "ting" (spectral signature of pellets hitting the bowl) per second of
        # motor run. The loud-spike count is kept as a secondary signal (a grinding motor also makes spikes).
        min_peaks_needed = max(15, self.cfg.min_kibble_audio_peaks)
        win_t0, win_t1 = w_start * 0.010, w_end * 0.010
        tings = [t for t in ting_times(samples, sample_rate) if win_t0 <= t <= win_t1] if motor_detected else []
        window_sec = max(0.0, win_t1 - win_t0)  # motor run + the 0.6 s falling-pellet tail
        ting_rate = len(tings) / window_sec if motor_detected and window_sec > 0 else 0.0
        kibble_dispensed = bool(
            motor_detected
            and (
                ting_rate >= self.cfg.ting_rate_min
                or (peaks >= min_peaks_needed and ting_rate >= self.cfg.ting_rate_min * 0.7)
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
                f"(+{motor_db:.1f} dB) without the kibble 'ting' ({ting_rate:.1f} tings/s < {self.cfg.ting_rate_min:.1f})."
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
            flow = kibble_flow(samples, sample_rate, res.motor_start_sec, res.motor_end_sec)
            if flow is not None:
                res.kibble_coverage = flow.coverage
                res.longest_gap_sec = flow.longest_gap_sec
                res.tail_gap_sec = flow.tail_gap_sec
                res.kibble_gaps = [list(g) for g in flow.gaps]
        if motor_detected:
            res.ml_prob_dispensed = _model_prob(res)
            if res.ml_prob_dispensed is not None and (res.ml_prob_dispensed >= 0.5) != kibble_dispensed:
                res.diagnosis += f" [learned model disagrees: p(food)={res.ml_prob_dispensed:.2f} — please review this clip]"
        return res
