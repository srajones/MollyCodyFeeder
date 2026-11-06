"""Pure helpers for the camera `imm` (AES/KCP 330) protocol: message framing, crypto, G.711 decoding."""

import hashlib
import hmac
import os
import random
import string
import struct
from typing import Optional, Tuple

import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# -----------------------------------------------------------------------------
# Native Molly & Cody `imm` (AES/KCP 330) Cloud Media & Two-Way Speaker Protocol
# -----------------------------------------------------------------------------
_IMM_MEDIA_MAGIC = 0x12345678


_IMM_CMD_VIDEO = 0x00010003


_IMM_CMD_AUDIO = 0x00010005


_IMM_SAMPLE_RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]


_IMM_STARTUP_MESSAGES = [
    bytes.fromhex("7856341200000000000000000a0000000400000000000100"),
    bytes.fromhex("785634120200000000000000020000000400000000000000"),
    bytes.fromhex("78563412040001000000000009000000080000000000000004000000"),  # 4 = 1280x720 HD
    bytes.fromhex("78563412030001000000000006000000080000000000000000000000"),
    bytes.fromhex("78563412050001000000000006000400080000000000000004000000"),
    bytes.fromhex("785634120600000000000000050000000400000000000000"),  # Open mic audio (conv=2)
]


# -----------------------------------------------------------------------------
# Two-way talk (speaker TX) framing — verified on-device via mic loopback (v0.1):
#   32-byte header + G.711 A-law payload, sent on KCP conv=2:
#   [0:4]   u32 0x12345678  magic
#   [4:8]   u32 talk reqId  MUST equal the reqId used in the talk-open (cmd=8) request,
#                           otherwise the camera ACKs the KCP segment but silently drops it
#   [8:12]  u32 0x86        codec (G.711 A-law) — as reported by the cmd=2 audio-param reply
#   [12:16] u32 0           sample-rate index (0 = 8000 Hz)
#   [16:20] u32 1           data bits (1 = 16-bit)
#   [20:24] u32 0           channels (0 = mono)
#   [24:28] u32 ms timestamp
#   [28:32] u32 payload length
# -----------------------------------------------------------------------------
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


def _build_alaw_table() -> np.ndarray:
    tbl = np.zeros(256, dtype=np.int16)
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
        tbl[b] = t if (val & 0x80) else -t
    return tbl


_ALAW_TO_PCM16_TABLE = _build_alaw_table()


def _alaw_to_pcm16(alaw_bytes: bytes) -> np.ndarray:
    if not alaw_bytes:
        return np.zeros(0, dtype=np.int16)
    idx = np.frombuffer(alaw_bytes, dtype=np.uint8)
    return _ALAW_TO_PCM16_TABLE[idx]


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
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + enc.update(padded) + enc.finalize()


def _imm_open_aes(key: bytes, data: bytes) -> bytes:
    if len(data) < 32 or (len(data) % 16) != 0:
        raise ValueError(f"bad cipher len {len(data)}")
    iv = data[:16]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data[16:]) + dec.finalize()
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
