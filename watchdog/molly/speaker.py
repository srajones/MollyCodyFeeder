"""Two-way talk: text-to-speech / audio files played through the feeder's speaker."""

import asyncio
import hashlib
import logging
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from watchdog.paths import DATA_DIR
from watchdog.molly.imm_codec import (
    _IMM_STARTUP_MESSAGES,
    _imm_build_ctrl,
    _imm_build_talk_frame,
    _imm_seal_aes,
)
from watchdog.molly.imm_transport import _ImmSessionProtocol

logger = logging.getLogger("poor_puppy.molly.speaker")


class SpeakerMixin:
    """Speaker output (TTS + audio files) over the camera talk channel."""

    def speak_tts(
        self,
        text: str,
        voice: str = "en-US-AriaNeural",
        volume: float = 2.5,
        repeat: int = 1,
        prepend_chime: bool = False,
        record_rx_wav: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Synthesizes `text` to speech (via neural `edge-tts`, `say`, or `espeak-ng`) and plays it
        through the smart feeder's built-in speaker over 100% Cloud `imm` (`AES/KCP 330`).
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "Empty TTS text"}

        repeat = max(1, min(5, int(repeat)))
        full_text = " ... ".join([text] * repeat)

        # Cache encoded alerts: repeated clog alerts then play instantly and still work when the
        # online neural TTS service is unreachable.
        cache_dir = Path(os.getenv("TTS_CACHE_DIR", str(DATA_DIR / "tts_cache")))
        key = hashlib.sha1(f"{full_text}|{voice}|{volume:.2f}|{int(prepend_chime)}|{self.TALK_FFMPEG_FORMAT}".encode("utf-8")).hexdigest()
        cache_file = cache_dir / f"{key}.{self.TALK_FFMPEG_FORMAT}"
        with tempfile.TemporaryDirectory(prefix="kibble_tts_") as tmpdir:
            if cache_file.exists() and cache_file.stat().st_size > 800:
                alaw_bytes = cache_file.read_bytes()
                engine = "cache"
            else:
                raw_audio = os.path.join(tmpdir, "tts_raw.mp3")
                engine = self._synthesize_tts_file(full_text, raw_audio, voice=voice)
                if not engine:
                    return {"ok": False, "error": "No TTS engine available (install edge-tts or espeak-ng)"}

                alaw_bytes = self._audio_file_to_alaw(
                    raw_audio,
                    volume=volume,
                    prepend_chime=prepend_chime,
                )
                if not alaw_bytes:
                    return {"ok": False, "error": "Failed to encode TTS audio to 8000 Hz G.711 A-law"}
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    files = sorted(cache_dir.glob(f"*.{self.TALK_FFMPEG_FORMAT}"), key=lambda f: f.stat().st_mtime)
                    for old_f in files[:-49]:  # keep the cache small (50 phrases)
                        old_f.unlink(missing_ok=True)
                    cache_file.write_bytes(alaw_bytes)
                except OSError as exc:
                    logger.debug("TTS cache write skipped: %s", exc)

            try:
                res = self._run_session(
                    self._imm_session_async(
                        duration_sec=0.0,
                        speaker_alaw_bytes=alaw_bytes,
                        output_wav=record_rx_wav,
                        decode_video=False,
                        timeout_sec=max(25.0, len(alaw_bytes) / 8000.0 + 18.0),
                    )
                )
                res["tts_engine"] = engine
                res["text"] = text
                res["duration_sec"] = round(len(alaw_bytes) / 8000.0, 2)
                return res
            except Exception as exc:
                logger.error("Cloud speaker TTS failed: %s", exc)
                return {"ok": False, "error": str(exc)}

    def play_audio_file(
        self,
        audio_path: str,
        volume: float = 2.5,
        prepend_chime: bool = False,
        record_rx_wav: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Streams an existing audio file (`.wav`, `.mp3`, `.m4a`, `.ogg`) through the smart feeder's
        built-in speaker over 100% Cloud `imm` (`AES/KCP 330`).
        """
        if not Path(audio_path).exists():
            return {"ok": False, "error": f"Audio file not found: {audio_path}"}
        alaw_bytes = self._audio_file_to_alaw(
            audio_path,
            volume=volume,
            prepend_chime=prepend_chime,
        )
        if not alaw_bytes:
            return {"ok": False, "error": f"Failed to convert {audio_path} to 8000 Hz G.711 A-law"}
        try:
            res = self._run_session(
                self._imm_session_async(
                    duration_sec=0.0,
                    speaker_alaw_bytes=alaw_bytes,
                    output_wav=record_rx_wav,
                    decode_video=False,
                    timeout_sec=max(20.0, len(alaw_bytes) / 8000.0 + 15.0),
                )
            )
            res["audio_path"] = audio_path
            res["duration_sec"] = round(len(alaw_bytes) / 8000.0, 2)
            return res
        except Exception as exc:
            logger.error("Cloud speaker audio playback failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _synthesize_tts_file(text: str, output_path: str, voice: str = "en-US-AriaNeural") -> Optional[str]:
        """Synthesizes `text` into `output_path` using `edge-tts`, macOS `say`, or Linux `espeak-ng`."""
        # 1. Try edge-tts (works on both Ubuntu VPS and macOS with neural voices)
        try:
            import edge_tts

            async def _run_edge():
                comm = edge_tts.Communicate(text, voice=voice)
                await comm.save(output_path)

            asyncio.run(_run_edge())
            if Path(output_path).exists() and Path(output_path).stat().st_size > 200:
                return f"edge-tts ({voice})"
        except Exception as exc:
            logger.debug("edge-tts synthesis fallback: %s", exc)

        # 2. Try macOS `say`
        if shutil.which("say"):
            aiff_path = output_path + ".aiff"
            try:
                subprocess.run(["say", "-o", aiff_path, text], check=True, timeout=15)
                if Path(aiff_path).exists() and Path(aiff_path).stat().st_size > 200:
                    os.replace(aiff_path, output_path)
                    return "macos-say"
            except Exception as exc:
                logger.debug("macOS say fallback failed: %s", exc)

        # 3. Try Linux `espeak-ng` or `espeak`
        for bin_name in ("espeak-ng", "espeak"):
            if shutil.which(bin_name):
                wav_path = output_path + ".wav"
                try:
                    subprocess.run([bin_name, "-s", "155", "-w", wav_path, text], check=True, timeout=15)
                    if Path(wav_path).exists() and Path(wav_path).stat().st_size > 200:
                        os.replace(wav_path, output_path)
                        return bin_name
                except Exception as exc:
                    logger.debug("%s synthesis failed: %s", bin_name, exc)

        return None

    @staticmethod
    def _build_chime_alaw(sample_rate: int = 8000) -> bytes:
        """Generates a crisp two-tone attention chime (880 Hz -> 1175 Hz + 0.2s pause) in G.711 A-law."""
        t1 = np.linspace(0, 0.18, int(sample_rate * 0.18), endpoint=False)
        t2 = np.linspace(0, 0.24, int(sample_rate * 0.24), endpoint=False)
        tone1 = 0.65 * np.sin(2 * np.pi * 880.0 * t1) * np.exp(-3.0 * t1)
        tone2 = 0.65 * np.sin(2 * np.pi * 1174.66 * t2) * np.exp(-2.5 * t2)
        pause = np.zeros(int(sample_rate * 0.18), dtype=np.float64)
        pcm = np.concatenate([tone1, tone2, pause])
        pcm16 = np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16)

        with tempfile.TemporaryDirectory(prefix="kibble_chime_") as tmpdir:
            wav_p = os.path.join(tmpdir, "chime.wav")
            alaw_p = os.path.join(tmpdir, "chime.alaw")
            with wave.open(wav_p, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm16.tobytes())
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_p, "-ar", "8000", "-ac", "1", "-f", SpeakerMixin.TALK_FFMPEG_FORMAT, alaw_p],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if Path(alaw_p).exists():
                return Path(alaw_p).read_bytes()
        return b""

    @staticmethod
    def _loudness_chain(volume: float) -> str:
        """ffmpeg filter chain that makes speech as loud as the small feeder speaker allows without clipping:
        drop lows it can't reproduce (headroom), normalize speech level, lift the 2.5 kHz presence band,
        apply gain, then a fast soft limiter (hard clipping is what sounds garbled)."""
        return (
            "highpass=f=250,lowpass=f=3600,"
            "dynaudnorm=f=150:g=7:p=0.95:m=12,"
            "equalizer=f=2500:t=q:w=1.2:g=5,"
            f"volume={max(0.1, volume):.2f},"
            "alimiter=limit=0.97:attack=2:release=40:level=false"
        )

    @classmethod
    def _audio_file_to_alaw(
        cls,
        audio_path: str,
        volume: float = 2.5,
        prepend_chime: bool = False,
    ) -> bytes:
        """Converts any audio file to 8000 Hz mono G.711 A-law (`PCMA`) bytes for `conv=2` speaker TX."""
        with tempfile.TemporaryDirectory(prefix="kibble_alaw_") as tmpdir:
            alaw_path = os.path.join(tmpdir, "out.alaw")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                audio_path,
                "-af",
                cls._loudness_chain(volume),
                "-ar",
                "8000",
                "-ac",
                "1",
                "-f",
                cls.TALK_FFMPEG_FORMAT,
                alaw_path,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if not Path(alaw_path).exists():
                return b""
            voice_alaw = Path(alaw_path).read_bytes()
            if prepend_chime:
                return cls._build_chime_alaw(8000) + voice_alaw
            return voice_alaw

    # Talk (speaker TX) tunables. Kept as class attributes so they can be tuned/tested
    # without editing the session code.
    TALK_REQ_ID = 7

    TALK_OPEN_CMD = 8

    TALK_OPEN_PAYLOAD = struct.pack("<II", 0, 0)

    TALK_CLOSE_CMD = 8

    TALK_CLOSE_PAYLOAD = struct.pack("<II", 0, 1)

    TALK_CONV = 2

    TALK_FRAME_BYTES = 320  # 40 ms of 8 kHz G.711
    # Verified on-device (mic loopback, 700 Hz tone): the speaker decodes talk audio as G.711 **mu-law**
    # even though the camera advertises codec 0x86. A-law payloads came out as static / robotic garble.
    TALK_FFMPEG_FORMAT = "mulaw"
    TALK_SILENCE_BYTE = b"\xff"  # mu-law digital silence (A-law would be 0xD5)

    async def _imm_talk(
        self,
        udp_proto: "_ImmSessionProtocol",
        alaw: bytes,
        output_wav: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Opens the talk channel and streams G.711 A-law to the feeder speaker, real-time paced."""
        key = udp_proto.aes_key

        def send_ctrl(raw: bytes) -> None:
            udp_proto.send_kcp_msg(0, _imm_seal_aes(key, raw))

        # Capability exchange + audio param query (skip HD video switch: it delays the talk ACK)
        for raw in _IMM_STARTUP_MESSAGES[:2]:
            send_ctrl(raw)
            await asyncio.sleep(0.10)
        if output_wav:
            # Mic RX only flows once the preview is started (video + audio), so start it fully
            for raw in _IMM_STARTUP_MESSAGES[2:]:
                send_ctrl(raw)
                await asyncio.sleep(0.10)

        req_id = self.TALK_REQ_ID
        send_ctrl(_imm_build_ctrl(req_id, self.TALK_OPEN_CMD, self.TALK_OPEN_PAYLOAD))
        try:
            await asyncio.wait_for(udp_proto.speaker_opened.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for talk-open ACK; proceeding with TX")
        await asyncio.sleep(0.30)  # let the speaker amplifier unmute

        n = self.TALK_FRAME_BYTES
        silence = self.TALK_SILENCE_BYTE
        padded = (silence * 1600) + alaw + (silence * 2400)  # 200 ms lead-in, 300 ms tail
        if len(padded) % n:
            padded += silence * (n - len(padded) % n)

        tx_start = time.monotonic()
        rtp_seq = random.randint(0, 0xFFFF)
        rtp_ts = random.randint(0, 0x7FFFFFFF)
        t0 = time.monotonic()
        frames_sent = 0
        for idx in range(0, len(padded), n):
            frame = _imm_build_talk_frame(
                rtp_seq + frames_sent,
                rtp_ts + frames_sent * n,
                int(time.time() * 1000),
                padded[idx : idx + n],
                req_id,
            )
            udp_proto.send_kcp_msg(self.TALK_CONV, _imm_seal_aes(key, frame))
            frames_sent += 1
            delay = frames_sent * (n / 8000.0) - (time.monotonic() - t0)
            if delay > 0:
                await asyncio.sleep(delay)

        tx_end = time.monotonic()
        t_wait = time.monotonic()
        while udp_proto.unacked.get(self.TALK_CONV) and (time.monotonic() - t_wait) < 3.0:
            await asyncio.sleep(0.08)
        await asyncio.sleep(0.8)  # device-side playout buffer

        send_ctrl(_imm_build_ctrl(req_id + 1, self.TALK_CLOSE_CMD, self.TALK_CLOSE_PAYLOAD))
        await asyncio.sleep(0.35)

        audio_ok = False
        if output_wav and udp_proto.audio_pcm_chunks:
            audio_ok = self._write_audio_wav(udp_proto.audio_pcm_chunks, udp_proto.audio_rate, output_wav)

        return {
            "ok": frames_sent > 0,
            "frames_sent": frames_sent,
            "speaker_opened": udp_proto.speaker_opened.is_set(),
            "unacked_remaining": len(udp_proto.unacked.get(self.TALK_CONV, {})),
            "remote_wnd": dict(udp_proto.remote_wnd),
            "tx_seconds": round(tx_end - tx_start, 2),
            "rx_audio_frames": len(udp_proto.audio_pcm_chunks),
            "rx_audio_ok": audio_ok,
            "ctrl_replies": [
                {"req": r, "flags": f, "cmd": c, "payload": p.hex()} for r, f, c, p in udp_proto.ctrl_replies
            ],
            "peer": f"{udp_proto.active_peer[0]}:{udp_proto.active_peer[1]}" if udp_proto.active_peer else "",
        }
