"""Recording helpers: motor-window detection on mic audio, frame selection, MP4/WAV writing."""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

try:
    from custom_components.molly_cody_feeder.core.analysis.vision import cv2
except Exception:
    cv2 = None  # type: ignore[assignment]

from custom_components.molly_cody_feeder.core.molly.imm_codec import (
    HAS_NUMPY,
    write_wav_pure_python,
)

try:
    import av  # type: ignore[import-not-found]
except ImportError:
    av = None  # type: ignore[assignment]

logger = logging.getLogger("molly_cody_feeder.molly.media")


def _chunk_rms_list(pcm_chunks: Sequence[Any]) -> List[float]:
    if not pcm_chunks:
        return []
    out: List[float] = []
    for c in pcm_chunks:
        if c is None or len(c) == 0:
            out.append(0.0)
        elif np is not None and HAS_NUMPY and isinstance(c, np.ndarray):
            arr = c.astype(np.float32) * (1.0 / 32768.0)
            out.append(float(np.sqrt(np.mean(arr * arr))))
        else:
            s2 = 0.0
            n = len(c)
            for v in c:
                fv = float(v) / 32768.0
                s2 += fv * fv
            out.append(math.sqrt(s2 / n) if n > 0 else 0.0)
    return out


def _chunk_activity(pcm_chunks: List[Any]) -> Any:
    rms_vals = _chunk_rms_list(pcm_chunks)
    if np is not None and HAS_NUMPY:
        if not rms_vals:
            return np.zeros(0, dtype=bool)
        return np.array(rms_vals, dtype=np.float32) > 0.005
    return [r > 0.005 for r in rms_vals]


def _motor_active_recently(pcm_chunks: List[Any], window_chunks: int = 30) -> bool:
    act = _chunk_activity(pcm_chunks[-window_chunks:])
    if len(act) < window_chunks // 2:
        return False
    mean_act = float(sum(1 for x in act if x)) / float(len(act))
    return mean_act > 0.6


def _motor_window(pcm_chunks: List[Any], rx_times: List[float]) -> Optional[Tuple[float, float]]:
    if not pcm_chunks or len(rx_times) != len(pcm_chunks):
        return None
    act = [1.0 if x else 0.0 for x in _chunk_activity(pcm_chunks)]
    n = len(act)
    if n == 0:
        return None
    # 6-point uniform boxcar filter with mode="same" (matching np.convolve(act, ones(6)/6, mode="same"))
    idx: List[int] = []
    for i in range(n):
        # For kernel of length 6, mode="same" centers with offset -2..+3
        s0 = max(0, i - 2)
        s1 = min(n, i + 4)
        if sum(act[s0:s1]) / 6.0 > 0.4:
            idx.append(i)
    if not idx:
        return None
    blocks: List[Tuple[int, int]] = []
    start = idx[0]
    prev = idx[0]
    for i in idx[1:]:
        if i - prev > 30:
            blocks.append((start, prev))
            start = i
        prev = i
    blocks.append((start, prev))
    sustained = [b for b in blocks if (b[1] - b[0]) >= 20]
    if not sustained:
        return None
    b0, b1 = max(sustained, key=lambda b: b[1] - b[0])
    return rx_times[b0] - 0.04, rx_times[b1]


def _save_frame_near(frames: List[Tuple[float, bytes]], t: float, out_path: str, prefer: str = "after") -> bool:
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
        elif Image is not None:
            Image.fromarray(payload).save(out_path, quality=92)
        else:
            return False
        return Path(out_path).exists()
    except Exception as exc:
        logger.debug("Frame save failed: %s", exc)
        return False


class MediaEncodingMixin:
    """MP4 / WAV writers for captured sessions."""

    @staticmethod
    def _encode_frames_to_mp4(
        frames: List[Tuple[float, Any]],
        output_mp4: str,
        audio_wav: Optional[str] = None,
    ) -> bool:
        if not frames or not output_mp4 or av is None or np is None or cv2 is None:
            return False
        try:
            def _rgb(payload: object) -> Optional[Any]:
                if isinstance(payload, (bytes, bytearray)):
                    bgr = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                    return None if bgr is None else cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                return payload

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
    def _write_audio_wav(
        chunks: Sequence[Any],
        sample_rate: int,
        output_wav: str,
        *,
        is_alaw: bool = False,
    ) -> bool:
        """Writes a 16-bit mono PCM WAV file in pure Python using stdlib `wave` (never spawns ffmpeg)."""
        ok = write_wav_pure_python(output_wav, chunks, sample_rate=sample_rate, is_alaw=is_alaw)
        if not ok:
            logger.error("Failed to write WAV %s", output_wav)
        return ok
