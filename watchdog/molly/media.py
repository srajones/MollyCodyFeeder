"""Recording helpers: motor-window detection on mic audio, frame selection, MP4/WAV writing."""

import logging
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

from watchdog.analysis.vision import cv2

try:
    import av  # type: ignore[import-not-found]
except ImportError:
    av = None  # type: ignore[assignment]

logger = logging.getLogger("poor_puppy.molly.media")


def _chunk_activity(pcm_chunks: List[np.ndarray]) -> np.ndarray:
    """Per-chunk (40 ms) activity flags using the analyzer's 'motor running' RMS gate."""
    if not pcm_chunks:
        return np.zeros(0, dtype=bool)
    rms = np.array([np.sqrt(np.mean((c.astype(np.float32) / 32768.0) ** 2)) if len(c) else 0.0 for c in pcm_chunks])
    return rms > 0.005


def _motor_active_recently(pcm_chunks: List[np.ndarray], window_chunks: int = 30) -> bool:
    """True if most of the last ~1.2 s of mic audio is active (motor still churning)."""
    act = _chunk_activity(pcm_chunks[-window_chunks:])
    return len(act) >= window_chunks // 2 and float(np.mean(act)) > 0.6


def _motor_window(pcm_chunks: List[np.ndarray], rx_times: List[float]) -> Optional[Tuple[float, float]]:
    """(onset, end) monotonic times of the first sustained (>= 0.8 s) active block, if any."""
    if not pcm_chunks or len(rx_times) != len(pcm_chunks):
        return None
    act = _chunk_activity(pcm_chunks).astype(np.float32)
    smooth = np.convolve(act, np.ones(6, dtype=np.float32) / 6.0, mode="same") > 0.4  # ~250 ms
    idx = np.where(smooth)[0]
    if len(idx) == 0:
        return None
    blocks, start, prev = [], int(idx[0]), int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > 30:  # > 1.2 s gap splits blocks
            blocks.append((start, prev))
            start = i
        prev = i
    blocks.append((start, prev))
    sustained = [b for b in blocks if (b[1] - b[0]) >= 20]
    if not sustained:
        return None
    b0, b1 = max(sustained, key=lambda b: b[1] - b[0])  # longest = the motor, not a bark before it
    # chunk arrival time ~ end of that 40 ms of audio
    return rx_times[b0] - 0.04, rx_times[b1]


def _save_frame_near(frames: List[Tuple[float, bytes]], t: float, out_path: str, prefer: str = "after") -> bool:
    """Writes the stored JPEG frame closest to monotonic time `t` (before/at t, or at/after t)."""
    if not frames:
        return False
    if prefer == "before":
        cands = [f for f in frames if f[0] <= t]
        pick = cands[-1] if cands else None
    else:
        cands = [f for f in frames if f[0] >= t]
        pick = cands[0] if cands else frames[-1]
    if pick is None:
        return False
    payload = pick[1]
    try:
        if isinstance(payload, (bytes, bytearray)):
            Path(out_path).write_bytes(payload)
        else:
            Image.fromarray(payload).save(out_path, quality=92)
        return Path(out_path).exists()
    except Exception as exc:
        logger.debug("Frame save failed: %s", exc)
        return False


class MediaEncodingMixin:
    """MP4 / WAV writers for captured sessions."""

    @staticmethod
    def _encode_frames_to_mp4(
        frames: List[Tuple[float, np.ndarray]],
        output_mp4: str,
        audio_wav: Optional[str] = None,
    ) -> bool:
        if not frames or av is None:
            return False
        try:
            def _rgb(payload):
                if isinstance(payload, (bytes, bytearray)):
                    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                    return None if bgr is None else cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                return payload

            # Keep only frames matching the dominant (HD) resolution; decode lazily (O(1) memory)
            last = _rgb(frames[-1][1])
            if last is None:
                return False
            target_h, target_w = last.shape[:2]
            filtered = (
                rgb for rgb in (_rgb(p) for _, p in frames)
                if rgb is not None and rgb.shape[0] == target_h and rgb.shape[1] == target_w
            )

            total_dur = max(1.0, frames[-1][0] - frames[0][0])
            fps = max(5, min(25, int(round(len(frames) / total_dur))))

            container = av.open(output_mp4, mode="w")
            stream = container.add_stream("libx264", rate=fps)
            stream.width = target_w
            stream.height = target_h
            stream.pix_fmt = "yuv420p"
            stream.options = {"preset": "ultrafast", "crf": "21"}

            # Precompute gamma=0.45 shadow-lifting LUT and smooth vertical blend for the backlit lower bowl zone
            gamma_lut = np.array([((i / 255.0) ** 0.45) * 255.0 for i in range(256)], dtype=np.uint8)
            y_start = int(target_h * 0.70)
            ramp_len = max(1, int(target_h * 0.08))
            alpha = np.ones((target_h - y_start, 1, 1), dtype=np.float32)
            alpha[:ramp_len, 0, 0] = np.linspace(0.0, 1.0, ramp_len, dtype=np.float32)

            for rgb in filtered:
                vis_rgb = rgb.copy()
                bottom = vis_rgb[y_start:, :, :]
                lifted = gamma_lut[bottom]
                vis_rgb[y_start:, :, :] = np.clip(
                    bottom.astype(np.float32) * (1.0 - alpha) + lifted.astype(np.float32) * alpha,
                    0,
                    255,
                ).astype(np.uint8)
                vframe = av.VideoFrame.from_ndarray(vis_rgb, format="rgb24")
                for packet in stream.encode(vframe):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
            container.close()

            if audio_wav and Path(audio_wav).exists() and shutil.which("ffmpeg"):
                muxed_tmp = output_mp4 + ".mux.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        output_mp4,
                        "-i",
                        audio_wav,
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "64k",
                        # No -shortest: the feeder's audio can drop out while video keeps coming. Cutting the
                        # video to the audio length hid the rest of the meal (and the AFTER frame came from
                        # footage that wasn't in the MP4). A shorter audio track is fine in MP4.
                        muxed_tmp,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if Path(muxed_tmp).exists() and Path(muxed_tmp).stat().st_size > 1000:
                    os.replace(muxed_tmp, output_mp4)

            return Path(output_mp4).exists() and Path(output_mp4).stat().st_size > 1000
        except Exception as exc:
            logger.error("Failed to encode MP4 %s: %s", output_mp4, exc)
            return False

    @staticmethod
    def _write_audio_wav(chunks: List[np.ndarray], sample_rate: int, output_wav: str) -> bool:
        try:
            all_samples = np.concatenate(chunks).astype(np.int16)
            with wave.open(output_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(all_samples.tobytes())
            return Path(output_wav).exists() and Path(output_wav).stat().st_size > 44
        except Exception as exc:
            logger.error("Failed to write WAV %s: %s", output_wav, exc)
            return False
