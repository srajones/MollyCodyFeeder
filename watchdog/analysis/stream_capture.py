"""ffmpeg capture from an external RTSP/HLS/FLV stream (alternative to the native camera session)."""

import logging
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("poor_puppy.analysis.stream_capture")


class StreamCaptureMixin:
    """Snapshot / clip capture with ffmpeg."""

    # -------------------------------------------------------------------------
    # FFmpeg Stream Capture (Video Frame & Audio Clip)
    # -------------------------------------------------------------------------
    def capture_snapshot(self, stream_url: str, output_path: str) -> bool:
        """Captures a single JPEG frame from the feeder's RTSP/HLS/FLV stream."""
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if stream_url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-i", stream_url, "-frames:v", "1", "-q:v", "2", output_path]
        try:
            subprocess.run(cmd, check=True, timeout=25)
            return Path(output_path).exists() and Path(output_path).stat().st_size > 0
        except Exception as exc:
            logger.error("Failed to capture snapshot from stream: %s", exc)
            return False

    def capture_av_clip(
        self, stream_url: str, duration_sec: int, output_mp4: str, output_wav: str
    ) -> Tuple[bool, bool]:
        """
        Records a short MP4 video clip AND extracts mono 16kHz WAV audio simultaneously
        during the feeding window so we can both SEE the food drop and HEAR it hit the bowl.
        """
        Path(output_mp4).parent.mkdir(parents=True, exist_ok=True)
        Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if stream_url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += [
            "-i",
            stream_url,
            "-t",
            str(duration_sec),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            output_mp4,
            "-t",
            str(duration_sec),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            output_wav,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=duration_sec + 20)
            v_ok = Path(output_mp4).exists() and Path(output_mp4).stat().st_size > 1000
            a_ok = Path(output_wav).exists() and Path(output_wav).stat().st_size > 44
            return v_ok, a_ok
        except Exception as exc:
            logger.warning("Simultaneous AV capture fell back to audio-only capture: %s", exc)
            a_ok = self.capture_audio_clip(stream_url, duration_sec, output_wav)
            return False, a_ok

    def capture_audio_clip(self, stream_url: str, duration_sec: int, output_wav: str) -> bool:
        """Records mono 16kHz WAV audio from the feeder camera microphone during dispense."""
        Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if stream_url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += [
            "-i",
            stream_url,
            "-t",
            str(duration_sec),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            output_wav,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=duration_sec + 15)
            return Path(output_wav).exists() and Path(output_wav).stat().st_size > 44
        except Exception as exc:
            logger.error("Failed to capture audio clip from stream: %s", exc)
            return False
