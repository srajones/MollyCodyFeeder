"""Pure helpers for the camera `imm` (AES/KCP 330) protocol: message framing, crypto, G.711 codec."""
from __future__ import annotations

import array
import hashlib
import hmac
import os
import random
import string
import struct
import wave
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

HAS_NUMPY: bool = np is not None

from custom_components.molly_cody_feeder.core.molly.crypto import (
    _aes128_cbc_decrypt,
    _aes128_cbc_encrypt,
)

# -----------------------------------------------------------------------------
# Native Molly & Cody `imm` (AES/KCP 330) Cloud Media & Two-Way Speaker Protocol
# -----------------------------------------------------------------------------
_IMM_MEDIA_MAGIC = 0x12345678
_IMM_CMD_VIDEO = 0x00010003
_IMM_CMD_AUDIO = 0x00010005
_IMM_CMD_START_VIDEO = 0x00000006
_IMM_CMD_START_AUDIO = 0x00040006
_IMM_SAMPLE_RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]

_IMM_STARTUP_MESSAGES = [
    bytes.fromhex("7856341200000000000000000a0000000400000000000100"),
    bytes.fromhex("785634120200000000000000020000000400000000000000"),
    bytes.fromhex("78563412040001000000000009000000080000000000000004000000"),  # 4 = 1280x720 HD
    bytes.fromhex("78563412030001000000000006000000080000000000000000000000"),  # cmd 6 = Start H.264 video (conv=1)
    bytes.fromhex("78563412050001000000000006000400080000000000000004000000"),  # cmd 0x00040006 = Start audio
    bytes.fromhex("785634120600000000000000050000000400000000000000"),  # Open mic audio (conv=2)
]

# Audio-only startup messages: omits cmd 9 (1280x720 video resolution) and cmd 6 (start H.264 video)
_IMM_AUDIO_ONLY_STARTUP_MESSAGES = [
    bytes.fromhex("7856341200000000000000000a0000000400000000000100"),
    bytes.fromhex("785634120200000000000000020000000400000000000000"),
    bytes.fromhex("78563412050001000000000006000400080000000000000004000000"),  # cmd 0x00040006 = Start audio
    bytes.fromhex("785634120600000000000000050000000400000000000000"),  # Open mic audio (conv=2)
]

_TALK_CODEC_G711A = 0x86


def _imm_build_talk_frame(seq: int, rtp_ts: int, ts_ms: int, alaw: bytes, req_id: int = 0) -> bytes:
    """Builds one speaker TX audio message (seq / rtp_ts kept for API compatibility)."""
    hdr = struct.pack(
        "<IIIIIIII",
        _IMM_MEDIA_MAGIC,
        req_id & 0xFFFFFFFF,
        _TALK_CODEC_G711A,
        0,
        1,
        0,
        ts_ms & 0xFFFFFFFF,
        len(alaw),
    )
    return hdr + alaw


def _imm_build_ctrl(req_id: int, cmd: int, payload: bytes) -> bytes:
    """Builds a conv=0 control request: magic, reqId, flags=0, cmd, len, payload."""
    return struct.pack("<IIIII", _IMM_MEDIA_MAGIC, req_id, 0, cmd, len(payload)) + payload


def _build_alaw_tuple() -> Tuple[int, ...]:
    vals = []
    for b in range(256):
        val = b ^ 0x55
        t = (val & 0x0F) << 4
        seg = (val & 0x70) >> 4
        if seg == 0:
            t += 8
        elif seg == 1:
            t += 0x108
        else:
            t += 0x108
            t <<= seg - 1
        vals.append(int(t if (val & 0x80) else -t))
    return tuple(vals)


_ALAW_TABLE: Tuple[int, ...] = _build_alaw_tuple()
_ALAW_PCM16_BYTES_TABLE: Tuple[bytes, ...] = tuple(struct.pack("<h", v) for v in _ALAW_TABLE)


def _build_alaw_table() -> Any:
    if np is not None:
        return np.array(_ALAW_TABLE, dtype=np.int16)
    return array.array("h", _ALAW_TABLE)


_ALAW_TO_PCM16_TABLE = _build_alaw_table()


def alaw_to_pcm16_bytes(alaw_bytes: bytes) -> bytes:
    """Converts G.711 A-law bytes into 16-bit little-endian PCM bytes in pure Python (zero numpy/ffmpeg)."""
    if not alaw_bytes:
        return b""
    tbl = _ALAW_PCM16_BYTES_TABLE
    return b"".join(tbl[b] for b in alaw_bytes)


def _alaw_to_pcm16(alaw_bytes: bytes) -> Any:
    if np is not None and HAS_NUMPY:
        if not alaw_bytes:
            return np.zeros(0, dtype=np.int16)
        idx = np.frombuffer(alaw_bytes, dtype=np.uint8)
        return _ALAW_TO_PCM16_TABLE[idx]
    out = array.array("h")
    if alaw_bytes:
        out.frombytes(alaw_to_pcm16_bytes(alaw_bytes))
    return out


def write_wav_pure_python(
    output_wav_path: str,
    chunks: Sequence[Any],
    sample_rate: int = 8000,
    *,
    is_alaw: bool = False,
) -> bool:
    """Writes a standard 16-bit mono PCM .wav file directly in pure Python using stdlib `wave` (zero ffmpeg)."""
    if not output_wav_path:
        return False
    try:
        pcm_parts = []
        for ch in chunks:
            if ch is None:
                continue
            if isinstance(ch, (bytes, bytearray, memoryview)):
                raw = bytes(ch)
                if raw:
                    pcm_parts.append(alaw_to_pcm16_bytes(raw) if is_alaw else raw)
            elif hasattr(ch, "dtype") and hasattr(ch, "astype") and hasattr(ch, "tobytes"):
                if len(ch) > 0:
                    pcm_parts.append(ch.astype("int16").tobytes())
            elif isinstance(ch, array.array):
                if len(ch) > 0:
                    if ch.typecode == "h":
                        pcm_parts.append(ch.tobytes())
                    else:
                        pcm_parts.append(array.array("h", (int(x) for x in ch)).tobytes())
            elif hasattr(ch, "__iter__"):
                vals = [int(x) for x in ch]
                if vals:
                    pcm_parts.append(struct.pack(f"<{len(vals)}h", *vals))
        pcm_bytes = b"".join(pcm_parts)
        if not pcm_bytes:
            return False
        out_p = Path(output_wav_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate or 8000))
            wf.writeframes(pcm_bytes)
        return out_p.is_file() and out_p.stat().st_size > 44
    except Exception:
        return False


def _build_ulaw_tuple() -> Tuple[int, ...]:
    vals = []
    for b in range(256):
        u = ~b & 0xFF
        sign = u & 0x80
        exp = (u >> 4) & 0x07
        mant = u & 0x0F
        sample = ((mant << 3) + 0x84) << exp
        sample -= 0x84
        vals.append(int(-sample if sign else sample))
    return tuple(vals)


_ULAW_TABLE: Tuple[int, ...] = _build_ulaw_tuple()


def _build_ulaw_table() -> Any:
    if np is not None:
        return np.array(_ULAW_TABLE, dtype=np.int16)
    return array.array("h", _ULAW_TABLE)


_ULAW_TO_PCM16_TABLE = _build_ulaw_table()


def _ulaw_to_pcm16(ulaw_bytes: bytes) -> Any:
    if np is not None and HAS_NUMPY:
        if not ulaw_bytes:
            return np.zeros(0, dtype=np.int16)
        idx = np.frombuffer(ulaw_bytes, dtype=np.uint8)
        return _ULAW_TO_PCM16_TABLE[idx]
    return array.array("h", (_ULAW_TABLE[b] for b in ulaw_bytes))


def _pcm16_to_ulaw(pcm16: Any) -> bytes:
    """ITU-T G.711 mu-law encoder in pure vectorized NumPy or pure-Python fallback (without ffmpeg)."""
    if pcm16 is None or len(pcm16) == 0:
        return b""
    if np is not None and HAS_NUMPY:
        samples = np.asarray(pcm16, dtype=np.int32)
        sign = np.where(samples < 0, 0x80, 0).astype(np.uint8)
        mag = np.minimum(np.abs(samples), 32635) + 0x84
        exp = np.zeros_like(mag, dtype=np.uint8)
        for e in range(1, 8):
            exp = np.where(mag >= (1 << (e + 7)), e, exp)
        mant = ((mag >> (exp + 3)) & 0x0F).astype(np.uint8)
        ulaw = (~(sign | (exp << 4) | mant)) & 0xFF
        return ulaw.astype(np.uint8).tobytes()
    out = bytearray(len(pcm16))
    for i, s in enumerate(pcm16):
        val = int(s)
        sign_b = 0x80 if val < 0 else 0
        m = min(abs(val), 32635) + 0x84
        e_val = 0
        for e in range(1, 8):
            if m >= (1 << (e + 7)):
                e_val = e
        mant_b = (m >> (e_val + 3)) & 0x0F
        out[i] = (~(sign_b | (e_val << 4) | mant_b)) & 0xFF
    return bytes(out)


def _imm_rand_ascii(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def _imm_login_token(device_password: str, local_key: str) -> str:
    return hashlib.md5(f"{device_password}||{local_key}".encode("utf-8")).hexdigest()


def _imm_build_login(token: str) -> bytes:
    msg = bytearray(8 + 32 + 32 + 32)
    struct.pack_into("<II", msg, 0, _IMM_MEDIA_MAGIC, 1)
    msg[8:13] = b"admin"
    tb = token.encode("ascii")[:32]
    msg[40 : 40 + len(tb)] = tb
    return bytes(msg)


def _imm_seal_aes(key: bytes, plain: bytes) -> bytes:
    pad = 16 - (len(plain) % 16)
    padded = plain + bytes([pad] * pad)
    iv = os.urandom(16)
    return iv + _aes128_cbc_encrypt(key, iv, padded)


def _imm_open_aes(key: bytes, data: bytes) -> bytes:
    if len(data) < 32 or (len(data) % 16) != 0:
        raise ValueError(f"bad cipher len {len(data)}")
    iv = data[:16]
    out = _aes128_cbc_decrypt(key, iv, data[16:])
    pad = out[-1]
    if 1 <= pad <= 16 and pad <= len(out):
        out = out[:-pad]
    return out


def _imm_mac_sha1(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha1).digest()


def _imm_is_stun(b: bytes) -> bool:
    return (
        len(b) >= 20
        and (b[0] & 0xC0) == 0
        and struct.unpack_from("!I", b, 4)[0] == 0x2112A442
    )


def _imm_media_payload(msg: bytes) -> Optional[Tuple[int, int, bool, bytes]]:
    if len(msg) < 32 + 8:
        return None
    cmd = struct.unpack_from("<I", msg, 0)[0]
    sub = msg[32:]
    if len(sub) >= 16:
        length = struct.unpack_from("<I", sub, 0)[0]
        if length >= 12 and (length - 12) == (len(sub) - 16):
            ts = struct.unpack_from("<Q", sub, 4)[0]
            return cmd, ts, True, sub[16:]
    payload = sub[8:]
    if not payload:
        return None
    ts = struct.unpack_from("<I", sub, 0)[0]
    return cmd, ts, False, payload
