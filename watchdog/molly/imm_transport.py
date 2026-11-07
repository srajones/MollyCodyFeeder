"""UDP/KCP transport for one camera session: ICE, KCP reliability, media demux and H.264 decode."""

import asyncio
import hmac
import logging
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from watchdog.analysis.vision import cv2
from watchdog.molly.imm_codec import (
    _IMM_CMD_AUDIO,
    _IMM_CMD_VIDEO,
    _IMM_MEDIA_MAGIC,
    _IMM_SAMPLE_RATES,
    _alaw_to_pcm16,
    _imm_is_stun,
    _imm_mac_sha1,
    _imm_media_payload,
    _imm_open_aes,
)

try:
    import av  # type: ignore[import-not-found]
except ImportError:
    av = None  # type: ignore[assignment]

try:
    from aioice import stun  # type: ignore[import-not-found]
except ImportError:
    stun = None  # type: ignore[assignment]

logger = logging.getLogger("poor_puppy.molly.imm_transport")


class _ImmSessionProtocol(asyncio.DatagramProtocol):
    """
    Implements the Molly & Cody `imm` (`AES/KCP 330`) UDP protocol over Cloud TURN/STUN:
    - `conv=0`: Control channel (login, HD switch, mic open, two-way speaker open/close)
    - `conv=1`: H.264 1280x720@15fps HD video stream (decoded & rotated 90° CW to 720x1280 portrait)
    - `conv=2`: 8000 Hz G.711 A-law (`PCMA`, codec 0x86) bidirectional audio (Mic RX + Speaker TX)
    """

    def __init__(
        self,
        local_ufrag: str,
        local_pwd: str,
        decode_video: bool = True,
        cloud_only: bool = True,
    ):
        self.local_ufrag = local_ufrag
        self.local_pwd = local_pwd
        self.cloud_only = cloud_only
        self.remote_ufrag = ""
        self.remote_pwd = ""
        self.aes_key = b""
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.ice_connected = asyncio.Event()
        self.any_video_ready = asyncio.Event()
        self.hd_video_ready = asyncio.Event()
        self.speaker_opened = asyncio.Event()
        self.active_peer: Optional[Tuple[str, int]] = None
        self.srflx_addr: Optional[Tuple[str, int]] = None
        self.stun_server_addr: Optional[Tuple[str, int]] = None

        self.rcv_nxt: Dict[int, int] = {}
        self.rcv_buf: Dict[int, Dict[int, Tuple[int, bytes]]] = {}
        self.partial_msg: Dict[int, List[bytes]] = {}
        self.unacked: Dict[int, Dict[int, bytes]] = {}
        self.snd_ts: Dict[int, Dict[int, float]] = {}
        self.snd_nxt: Dict[int, int] = {}

        self.decode_video = decode_video and (av is not None)
        self.vcodec: Optional[Any] = av.CodecContext.create("h264", "r") if self.decode_video else None
        self._au_buf = bytearray()
        self._seen_sps = False
        self.video_frames: List[Tuple[float, bytes]] = []  # (monotonic ts, JPEG bytes)
        self.latest_rgb: Optional[np.ndarray] = None
        self.video_packet_count = 0
        self.audio_alaw_chunks: List[bytes] = []
        self.audio_pcm_chunks: List[np.ndarray] = []
        self.audio_rate = 8000
        self.audio_codec = 0x86
        self.recording_active = True
        self.ctrl_replies: List[Tuple[int, int, int, bytes]] = []
        self.remote_wnd: Dict[int, int] = {}
        self.audio_rx_times: List[float] = []

    def connection_made(self, transport):
        self.transport = transport

    def send_raw(self, data: bytes, addr: Optional[Tuple[str, int]]):
        if self.transport is not None and addr is not None:
            self.transport.sendto(data, addr)

    def send_kcp_seg(self, conv: int, cmd: int, frg: int, ts: int, sn: int, payload: bytes):
        if not self.aes_key or not self.active_peer:
            return
        una = self.rcv_nxt.get(conv, 0)
        hdr = struct.pack("<IBBHIIII", conv, cmd, frg, 512, ts & 0xFFFFFFFF, sn, una, len(payload))
        seg = hdr + payload
        pkt = seg + _imm_mac_sha1(self.aes_key, seg)
        self.send_raw(pkt, self.active_peer)

    def send_kcp_msg(self, conv: int, payload: bytes):
        sn = self.snd_nxt.get(conv, 0)
        self.snd_nxt[conv] = sn + 1
        if conv not in self.unacked:
            self.unacked[conv] = {}
        if conv not in self.snd_ts:
            self.snd_ts[conv] = {}
        self.unacked[conv][sn] = payload
        self.snd_ts[conv][sn] = time.monotonic()
        now_ms = int(time.time() * 1000) & 0xFFFFFFFF
        self.send_kcp_seg(conv, 81, 0, now_ms, sn, payload)

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        if self.cloud_only and addr[0].startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.3")):
            return
        if _imm_is_stun(data) and stun is not None:
            try:
                msg = stun.parse_message(data)
            except Exception:
                return
            if msg.message_class == stun.Class.RESPONSE and msg.message_method == stun.Method.BINDING:
                if addr == self.stun_server_addr:
                    self.srflx_addr = msg.attributes.get("XOR-MAPPED-ADDRESS")
                    return
                if not self.ice_connected.is_set():
                    self.active_peer = addr
                    self.ice_connected.set()
            elif msg.message_class == stun.Class.REQUEST and msg.message_method == stun.Method.BINDING:
                resp = stun.Message(
                    message_method=stun.Method.BINDING,
                    message_class=stun.Class.RESPONSE,
                    transaction_id=msg.transaction_id,
                )
                resp.attributes["XOR-MAPPED-ADDRESS"] = addr
                resp.add_message_integrity(self.local_pwd.encode("utf-8"))
                self.send_raw(bytes(resp), addr)
                if not self.ice_connected.is_set():
                    self.active_peer = addr
                    self.ice_connected.set()
            return

        if not self.aes_key or len(data) < 20 + 24:
            return

        body = data[:-20]
        tag = data[-20:]
        if not hmac.compare_digest(_imm_mac_sha1(self.aes_key, body), tag):
            return

        if not self.ice_connected.is_set():
            self.active_peer = addr
            self.ice_connected.set()

        off = 0
        ack_segments = []
        while off + 24 <= len(body):
            conv, cmd, frg, wnd, ts, sn, una, length = struct.unpack_from("<IBBHIIII", body, off)
            off += 24
            if off + length > len(body):
                break
            seg_data = body[off : off + length]
            off += length
            self.remote_wnd[conv] = wnd

            if conv in self.unacked:
                for k in list(self.unacked[conv].keys()):
                    if k < una:
                        del self.unacked[conv][k]
                        if conv in self.snd_ts:
                            self.snd_ts[conv].pop(k, None)

            if cmd == 82:  # ACK
                if conv in self.unacked and sn in self.unacked[conv]:
                    del self.unacked[conv][sn]
                if conv in self.snd_ts:
                    self.snd_ts[conv].pop(sn, None)
            elif cmd == 83:  # WASK
                now_ms = int(time.time() * 1000) & 0xFFFFFFFF
                self.send_kcp_seg(conv, 84, 0, now_ms, 0, b"")
            elif cmd == 81:  # PUSH
                if conv not in self.rcv_nxt:
                    self.rcv_nxt[conv] = 0
                    self.rcv_buf[conv] = {}
                    self.partial_msg[conv] = []

                if sn >= self.rcv_nxt[conv] and sn not in self.rcv_buf[conv]:
                    self.rcv_buf[conv][sn] = (frg, seg_data)

                while self.rcv_nxt[conv] in self.rcv_buf[conv]:
                    f_num, part = self.rcv_buf[conv].pop(self.rcv_nxt[conv])
                    self.rcv_nxt[conv] += 1
                    self.partial_msg[conv].append(part)
                    if f_num == 0:
                        cipher_msg = b"".join(self.partial_msg[conv])
                        self.partial_msg[conv].clear()
                        self._on_kcp_message(conv, cipher_msg)

                una_now = self.rcv_nxt[conv]
                ack_hdr = struct.pack("<IBBHIIII", conv, 82, 0, 512, ts, sn, una_now, 0)
                ack_segments.append(ack_hdr)

        if ack_segments:
            compound_ack = b"".join(ack_segments)
            pkt = compound_ack + _imm_mac_sha1(self.aes_key, compound_ack)
            self.send_raw(pkt, addr)

    def _on_kcp_message(self, conv: int, cipher_msg: bytes):
        try:
            msg = _imm_open_aes(self.aes_key, cipher_msg)
        except Exception:
            return
        if len(msg) < 4:
            return
        magic_or_cmd = struct.unpack_from("<I", msg, 0)[0]
        if magic_or_cmd == _IMM_MEDIA_MAGIC:
            if len(msg) >= 20:
                seq, flags, ctrl_cmd, length = struct.unpack_from("<IIII", msg, 4)
                self.ctrl_replies.append((seq, flags, ctrl_cmd, bytes(msg[20 : 20 + length])))
                if ctrl_cmd == 8:
                    self.speaker_opened.set()
            return

        if magic_or_cmd == _IMM_CMD_VIDEO:
            self.video_packet_count += 1
            if not self.decode_video or self.vcodec is None or len(msg) < 49:
                return
            parsed = _imm_media_payload(msg)
            if not parsed:
                return
            _, _, _, payload = parsed
            if not payload:
                return

            nal_type = payload[0] & 0x1F
            flush_frame = False
            if 1 <= nal_type <= 23:
                if nal_type == 7:
                    self._seen_sps = True
                if self._seen_sps:
                    self._au_buf.extend(b"\x00\x00\x00\x01")
                    self._au_buf.extend(payload)
                    if nal_type in (1, 5):
                        flush_frame = True
            elif nal_type == 28 and len(payload) >= 2:  # RFC 6184 FU-A
                fu_ind = payload[0]
                fu_hdr = payload[1]
                start_bit = (fu_hdr & 0x80) != 0
                end_bit = (fu_hdr & 0x40) != 0
                orig_type = fu_hdr & 0x1F
                if orig_type == 7:
                    self._seen_sps = True
                if self._seen_sps:
                    if start_bit:
                        reconstructed_hdr = (fu_ind & 0xE0) | orig_type
                        self._au_buf.extend(b"\x00\x00\x00\x01")
                        self._au_buf.append(reconstructed_hdr)
                    self._au_buf.extend(payload[2:])
                    if end_bit and orig_type in (1, 5):
                        flush_frame = True

            if flush_frame and self._au_buf and av is not None:
                au_bytes = bytes(self._au_buf)
                self._au_buf.clear()
                try:
                    for frame in self.vcodec.decode(av.Packet(au_bytes)):
                        rgb = frame.to_ndarray(format="rgb24")
                        # Rotate 90 degrees clockwise to match upright portrait orientation (720x1280)
                        rgb = np.ascontiguousarray(np.rot90(rgb, k=3))
                        self.latest_rgb = rgb
                        if self.recording_active:
                            # JPEG-compress (~0.1 MB) instead of keeping raw RGB (~2.7 MB/frame):
                            # a 45 s multi-portion feed would otherwise need ~1.8 GB of RAM.
                            ok_jpg, jpg = cv2.imencode(
                                ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [int(cv2.IMWRITE_JPEG_QUALITY), 90]
                            )
                            if ok_jpg:
                                self.video_frames.append((time.monotonic(), jpg.tobytes()))
                        self.any_video_ready.set()
                        if max(frame.width, frame.height) >= 1280 and len(self.video_frames) >= 2:
                            self.hd_video_ready.set()
                except Exception as exc:
                    logger.debug("Video decode warning: %s", exc)
            return

        if magic_or_cmd == _IMM_CMD_AUDIO:
            parsed = _imm_media_payload(msg)
            if not parsed:
                return
            _, _, _, payload = parsed
            codec = msg[24]
            rate_idx = struct.unpack_from("<H", msg, 26)[0]
            self.audio_rate = (
                _IMM_SAMPLE_RATES[rate_idx] if rate_idx < len(_IMM_SAMPLE_RATES) else 8000
            )
            self.audio_codec = codec
            if self.recording_active and payload:
                self.audio_rx_times.append(time.monotonic())
                self.audio_alaw_chunks.append(payload)
                self.audio_pcm_chunks.append(_alaw_to_pcm16(payload))
