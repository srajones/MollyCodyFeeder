"""Request signing, password encryption and MQTT protocol-2.2 payload encryption.

Uses `cryptography` when installed, with a genuine FIPS-197 AES-128 (ECB/CBC) and RFC 8017
RSA PKCS#1 v1.5 pure-Python fallback for minimal environments.
"""

import hashlib
import hmac
import json
import os
import struct
import zlib
from typing import Any, Dict, List, Optional

from watchdog.molly.constants import APP_ID, CERT_SHA256, PKG_NAME, SIGNED_KEYS

try:
    from cryptography.hazmat.primitives.asymmetric import padding as _rsa_padding, rsa as _rsa
    from cryptography.hazmat.primitives.ciphers import Cipher as _Cipher, algorithms as _algorithms, modes as _modes

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False


# ---------------------------------------------------------------------------
# Pure-Python FIPS-197 AES-128 & RSA PKCS#1 v1.5 fallback
# ---------------------------------------------------------------------------
_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_INV_SBOX = tuple(_AES_SBOX.index(i) for i in range(256))
_RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1)


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _aes128_expand_key(key: bytes) -> List[List[int]]:
    if len(key) != 16:
        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
    w: List[List[int]] = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = [_AES_SBOX[temp[(j + 1) % 4]] for j in range(4)]
            temp[0] ^= _RCON[i // 4]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(w[4 * r : 4 * (r + 1)], []) for r in range(11)]


def _aes128_encrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    s = [block[i] ^ round_keys[0][i] for i in range(16)]
    for r in range(1, 10):
        # SubBytes + ShiftRows (column-major state)
        s = [
            _AES_SBOX[s[0]], _AES_SBOX[s[5]], _AES_SBOX[s[10]], _AES_SBOX[s[15]],
            _AES_SBOX[s[4]], _AES_SBOX[s[9]], _AES_SBOX[s[14]], _AES_SBOX[s[3]],
            _AES_SBOX[s[8]], _AES_SBOX[s[13]], _AES_SBOX[s[2]], _AES_SBOX[s[7]],
            _AES_SBOX[s[12]], _AES_SBOX[s[1]], _AES_SBOX[s[6]], _AES_SBOX[s[11]],
        ]
        # MixColumns + AddRoundKey
        rk = round_keys[r]
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            t = a0 ^ a1 ^ a2 ^ a3
            s[c] = a0 ^ t ^ _xtime(a0 ^ a1) ^ rk[c]
            s[c + 1] = a1 ^ t ^ _xtime(a1 ^ a2) ^ rk[c + 1]
            s[c + 2] = a2 ^ t ^ _xtime(a2 ^ a3) ^ rk[c + 2]
            s[c + 3] = a3 ^ t ^ _xtime(a3 ^ a0) ^ rk[c + 3]
    rk = round_keys[10]
    s = [
        _AES_SBOX[s[0]] ^ rk[0], _AES_SBOX[s[5]] ^ rk[1], _AES_SBOX[s[10]] ^ rk[2], _AES_SBOX[s[15]] ^ rk[3],
        _AES_SBOX[s[4]] ^ rk[4], _AES_SBOX[s[9]] ^ rk[5], _AES_SBOX[s[14]] ^ rk[6], _AES_SBOX[s[3]] ^ rk[7],
        _AES_SBOX[s[8]] ^ rk[8], _AES_SBOX[s[13]] ^ rk[9], _AES_SBOX[s[2]] ^ rk[10], _AES_SBOX[s[7]] ^ rk[11],
        _AES_SBOX[s[12]] ^ rk[12], _AES_SBOX[s[1]] ^ rk[13], _AES_SBOX[s[6]] ^ rk[14], _AES_SBOX[s[11]] ^ rk[15],
    ]
    return bytes(s)


def _aes128_decrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    s = [block[i] ^ round_keys[10][i] for i in range(16)]
    for r in range(9, 0, -1):
        # InvShiftRows + InvSubBytes
        s = [
            _AES_INV_SBOX[s[0]], _AES_INV_SBOX[s[13]], _AES_INV_SBOX[s[10]], _AES_INV_SBOX[s[7]],
            _AES_INV_SBOX[s[4]], _AES_INV_SBOX[s[1]], _AES_INV_SBOX[s[14]], _AES_INV_SBOX[s[11]],
            _AES_INV_SBOX[s[8]], _AES_INV_SBOX[s[5]], _AES_INV_SBOX[s[2]], _AES_INV_SBOX[s[15]],
            _AES_INV_SBOX[s[12]], _AES_INV_SBOX[s[9]], _AES_INV_SBOX[s[6]], _AES_INV_SBOX[s[3]],
        ]
        rk = round_keys[r]
        s = [s[i] ^ rk[i] for i in range(16)]
        # InvMixColumns
        for c in range(0, 16, 4):
            a0, a1, a2, a3 = s[c], s[c + 1], s[c + 2], s[c + 3]
            s[c] = _gmul(a0, 0x0E) ^ _gmul(a1, 0x0B) ^ _gmul(a2, 0x0D) ^ _gmul(a3, 0x09)
            s[c + 1] = _gmul(a0, 0x09) ^ _gmul(a1, 0x0E) ^ _gmul(a2, 0x0B) ^ _gmul(a3, 0x0D)
            s[c + 2] = _gmul(a0, 0x0D) ^ _gmul(a1, 0x09) ^ _gmul(a2, 0x0E) ^ _gmul(a3, 0x0B)
            s[c + 3] = _gmul(a0, 0x0B) ^ _gmul(a1, 0x0D) ^ _gmul(a2, 0x09) ^ _gmul(a3, 0x0E)
    rk = round_keys[0]
    s = [
        _AES_INV_SBOX[s[0]] ^ rk[0], _AES_INV_SBOX[s[13]] ^ rk[1], _AES_INV_SBOX[s[10]] ^ rk[2], _AES_INV_SBOX[s[7]] ^ rk[3],
        _AES_INV_SBOX[s[4]] ^ rk[4], _AES_INV_SBOX[s[1]] ^ rk[5], _AES_INV_SBOX[s[14]] ^ rk[6], _AES_INV_SBOX[s[11]] ^ rk[7],
        _AES_INV_SBOX[s[8]] ^ rk[8], _AES_INV_SBOX[s[5]] ^ rk[9], _AES_INV_SBOX[s[2]] ^ rk[10], _AES_INV_SBOX[s[15]] ^ rk[11],
        _AES_INV_SBOX[s[12]] ^ rk[12], _AES_INV_SBOX[s[9]] ^ rk[13], _AES_INV_SBOX[s[6]] ^ rk[14], _AES_INV_SBOX[s[3]] ^ rk[15],
    ]
    return bytes(s)


def _aes128_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    if _HAS_CRYPTOGRAPHY:
        enc = _Cipher(_algorithms.AES(key), _modes.ECB()).encryptor()
        return enc.update(data) + enc.finalize()
    rks = _aes128_expand_key(key)
    return b"".join(_aes128_encrypt_block(data[i : i + 16], rks) for i in range(0, len(data), 16))


def _aes128_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    if _HAS_CRYPTOGRAPHY:
        dec = _Cipher(_algorithms.AES(key), _modes.ECB()).decryptor()
        return dec.update(data) + dec.finalize()
    rks = _aes128_expand_key(key)
    return b"".join(_aes128_decrypt_block(data[i : i + 16], rks) for i in range(0, len(data), 16))


def _aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if _HAS_CRYPTOGRAPHY:
        enc = _Cipher(_algorithms.AES(key), _modes.CBC(iv)).encryptor()
        return enc.update(data) + enc.finalize()
    rks = _aes128_expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        xored = bytes(data[i + j] ^ prev[j] for j in range(16))
        prev = _aes128_encrypt_block(xored, rks)
        out.extend(prev)
    return bytes(out)


def _aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if _HAS_CRYPTOGRAPHY:
        dec = _Cipher(_algorithms.AES(key), _modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    rks = _aes128_expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        blk = data[i : i + 16]
        dec_blk = _aes128_decrypt_block(blk, rks)
        out.extend(dec_blk[j] ^ prev[j] for j in range(16))
        prev = blk
    return bytes(out)


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().lower()


def _get_ch_key() -> str:
    msg = f"{PKG_NAME}_{CERT_SHA256}".encode("utf-8")
    digest = hmac.new(APP_ID.encode("utf-8"), msg, hashlib.sha256).hexdigest().lower()
    return digest[8:16]


def _post_data_md5_hex(post_data: str) -> str:
    if not post_data:
        return ""
    s = _md5_hex(post_data)
    return s[8:16] + s[0:8] + s[24:32] + s[16:24]


def _build_sign_input(params: Dict[str, Any]) -> str:
    parts = []
    for k in sorted(params.keys()):
        if k not in SIGNED_KEYS:
            continue
        v = str(params.get(k, ""))
        if not v:
            continue
        if k == "postData":
            v = _post_data_md5_hex(v)
        parts.append(f"{k}={v}")
    return "||".join(parts)


def _rsa_encrypt_password(password: str, pub_key_dec: str, exp_dec: str) -> str:
    md5_pwd = _md5_hex(password).encode("utf-8")
    n = int(pub_key_dec)
    e = int(exp_dec)
    if _HAS_CRYPTOGRAPHY:
        pub_key = _rsa.RSAPublicNumbers(e, n).public_key()
        encrypted = pub_key.encrypt(md5_pwd, _rsa_padding.PKCS1v15())
        return encrypted.hex().lower()
    # RFC 8017 Section 7.2.1 RSAES-PKCS1-v1_5 encryption
    k = (n.bit_length() + 7) // 8
    ps_len = k - len(md5_pwd) - 3
    if ps_len < 8:
        raise ValueError("RSA key modulus too short for PKCS#1 v1.5")
    ps = bytearray()
    while len(ps) < ps_len:
        b = os.urandom(1)[0]
        if b != 0:
            ps.append(b)
    em = b"\x00\x02" + bytes(ps) + b"\x00" + md5_pwd
    c = pow(int.from_bytes(em, "big"), e, n)
    return c.to_bytes(k, "big").hex().lower()


def _pkcs7_pad(data: bytes) -> bytes:
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if 1 <= pad_len <= 16:
        return data[:-pad_len]
    return data


def _encrypt_mqtt_22(payload_json: str, local_key: str, seq: int = 1, source: int = 1) -> bytes:
    raw = _pkcs7_pad(payload_json.encode("utf-8"))
    ct = _aes128_ecb_encrypt(local_key[:16].encode("utf-8"), raw)
    seq_src = struct.pack(">II", seq, source)
    crc = zlib.crc32(seq_src + ct) & 0xFFFFFFFF
    return b"2.2" + struct.pack(">I", crc) + seq_src + ct


def _decrypt_mqtt_22(pkt: bytes, local_key: str) -> Optional[Dict[str, Any]]:
    if not pkt.startswith(b"2.2") or len(pkt) <= 15:
        return None
    ct = pkt[15:]
    pt = _pkcs7_unpad(_aes128_ecb_decrypt(local_key[:16].encode("utf-8"), ct))
    return json.loads(pt.decode("utf-8", errors="ignore"))
