"""Request signing, password encryption and MQTT protocol-2.2 payload encryption."""

import hashlib
import hmac
import json
import struct
import zlib
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from watchdog.molly.constants import APP_ID, CERT_SHA256, PKG_NAME, SIGNED_KEYS


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
    pub_key = rsa.RSAPublicNumbers(e, n).public_key()
    encrypted = pub_key.encrypt(md5_pwd, padding.PKCS1v15())
    return encrypted.hex().lower()


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
    cipher = Cipher(algorithms.AES(local_key[:16].encode("utf-8")), modes.ECB())
    enc = cipher.encryptor()
    ct = enc.update(raw) + enc.finalize()
    seq_src = struct.pack(">II", seq, source)
    crc = zlib.crc32(seq_src + ct) & 0xFFFFFFFF
    return b"2.2" + struct.pack(">I", crc) + seq_src + ct


def _decrypt_mqtt_22(pkt: bytes, local_key: str) -> Optional[Dict[str, Any]]:
    if not pkt.startswith(b"2.2") or len(pkt) <= 15:
        return None
    ct = pkt[15:]
    cipher = Cipher(algorithms.AES(local_key[:16].encode("utf-8")), modes.ECB())
    dec = cipher.decryptor()
    pt = _pkcs7_unpad(dec.update(ct) + dec.finalize())
    return json.loads(pt.decode("utf-8", errors="ignore"))
