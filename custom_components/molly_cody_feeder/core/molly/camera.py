"""Camera sessions over the cloud `imm` protocol: snapshots and audio/video verification clips."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

from custom_components.molly_cody_feeder.core.molly.constants import (
    MQTT_HOST,
    MQTT_PORT,
)
from custom_components.molly_cody_feeder.core.molly.imm_codec import (
    _ALAW_TABLE,
    _IMM_AUDIO_ONLY_STARTUP_MESSAGES,
    _IMM_STARTUP_MESSAGES,
    _imm_build_login,
    _imm_login_token,
    _imm_rand_ascii,
    _imm_seal_aes,
    alaw_to_pcm16_bytes,
    write_wav_pure_python,
)
from custom_components.molly_cody_feeder.core.molly.imm_transport import (
    _ImmSessionProtocol,
)
from custom_components.molly_cody_feeder.core.molly.media import (
    _motor_active_recently,
    _motor_window,
    _save_frame_near,
)

try:
    from aioice import stun  # type: ignore[import-not-found]
except ImportError:
    stun = None  # type: ignore[assignment]

logger = logging.getLogger("molly_cody_feeder.molly.camera")


def _recording_coverage(
    pcm_chunks: List[Any], audio_rate: int, rx_times: List[float], rec_start: float, rec_stop: float
) -> Dict[str, Any]:
    samples = sum(len(c) for c in pcm_chunks)
    rate = float(audio_rate or 8000)
    return {
        "recorded_wall_sec": round(max(0.0, rec_stop - rec_start), 2),
        "audio_sec": round(samples / rate, 2),
        "audio_last_rx_sec": round(rx_times[-1] - rec_start, 2) if rx_times else None,
    }


class CameraMixin:
    """Snapshots and recorded verification clips (one camera session at a time)."""

    def capture_snapshot(self, output_path: str, timeout_sec: float = 15.0) -> bool:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            res = self._run_session(
                self._imm_session_async(
                    duration_sec=0.0,
                    snapshot_only_path=output_path,
                    timeout_sec=timeout_sec,
                )
            )
            if res.get("snapshot_ok"):
                return True
        except Exception as exc:
            logger.warning("Cloud imm snapshot capture failed (%s), trying WebRTC fallback...", exc)

        try:
            res = self._run_session(
                self._webrtc_session_async(
                    duration_sec=0.0,
                    snapshot_only_path=output_path,
                    timeout_sec=timeout_sec,
                )
            )
            return bool(res.get("snapshot_ok"))
        except Exception as exc:
            logger.error("Snapshot capture failed: %s", exc)
            return False

    def capture_av_clip(
        self,
        duration_sec: float = 20.0,
        output_mp4: Optional[str] = "",
        output_wav: Optional[str] = "",
        before_jpg: Optional[str] = None,
        after_jpg: Optional[str] = None,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 1.2,
        decode_video: bool = True,
        audio_only: bool = False,
        *,
        output_mp4_path: Optional[str] = None,
        output_wav_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        if output_mp4_path is not None:
            output_mp4 = output_mp4_path
        if output_wav_path is not None:
            output_wav = output_wav_path
        mp4_str = str(output_mp4 or "") if not audio_only else ""
        wav_str = str(output_wav or "")
        effective_decode_video = (not audio_only) and bool(decode_video)

        if mp4_str:
            Path(mp4_str).parent.mkdir(parents=True, exist_ok=True)
        if wav_str:
            Path(wav_str).parent.mkdir(parents=True, exist_ok=True)
        if before_jpg and not audio_only:
            Path(before_jpg).parent.mkdir(parents=True, exist_ok=True)
        if after_jpg and not audio_only:
            Path(after_jpg).parent.mkdir(parents=True, exist_ok=True)

        fired: Dict[str, Any] = {"v": False, "ok": True, "detail": ""}

        def _guarded_trigger() -> Any:
            fired["v"] = True
            if not trigger_fn:
                return None
            res_trig = trigger_fn()
            if isinstance(res_trig, tuple) and len(res_trig) >= 2:
                fired["ok"] = bool(res_trig[0])
                fired["detail"] = str(res_trig[1])
            elif isinstance(res_trig, bool):
                fired["ok"] = res_trig
            return res_trig

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                res = self._run_session(
                    self._imm_session_async(
                        duration_sec=duration_sec,
                        output_mp4=mp4_str,
                        output_wav=wav_str,
                        before_jpg=before_jpg if not audio_only else None,
                        after_jpg=after_jpg if not audio_only else None,
                        trigger_fn=_guarded_trigger if trigger_fn else None,
                        trigger_delay_sec=trigger_delay_sec,
                        decode_video=effective_decode_video,
                        audio_only=bool(audio_only),
                        timeout_sec=duration_sec + 22.0,
                    )
                )
                if isinstance(res, dict):
                    res.setdefault("trigger_ok", fired["ok"])
                    res.setdefault("trigger_detail", fired["detail"])
                    res.setdefault("audio_only", bool(audio_only))
                return res
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and not fired["v"]:
                    logger.warning("Camera session failed (%s); retrying once in 3s...", exc)
                    time.sleep(3.0)
                    continue
                break
        if last_exc is not None:
            exc = last_exc
            logger.error("Cloud imm AV clip capture failed: %s", exc)
            dispensed_unverified = False
            if trigger_fn is not None and not fired["v"]:
                logger.warning("Camera unavailable: dispensing WITHOUT video verification")
                try:
                    _guarded_trigger()
                    dispensed_unverified = True
                except Exception as trig_exc:
                    logger.error("Fallback dispense failed: %s", trig_exc)
            return {
                "video_ok": False,
                "audio_ok": False,
                "wav_saved": False,
                "mp4_saved": False,
                "before_ok": False,
                "after_ok": False,
                "video_frames": 0,
                "audio_frames": 0,
                "audio_bytes": 0,
                "motor_duration_sec": 0.0,
                "trigger_ok": dispensed_unverified or (fired["v"] and fired["ok"]),
                "trigger_detail": fired["detail"] or ("dispensed_unverified" if dispensed_unverified else str(exc)),
                "audio_only": bool(audio_only),
                "error": str(exc),
                "dispensed_unverified": dispensed_unverified,
            }
        return {}

    def capture_audio_clip(
        self,
        output_wav_path: str,
        duration_sec: float = 20.0,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 3.0,
    ) -> Dict[str, Any]:
        """Captures an audio-only 8000 Hz 16-bit PCM WAV clip over Cloud `imm` without video or ffmpeg."""
        res = self.capture_av_clip(
            output_mp4_path="",
            output_wav_path=output_wav_path,
            duration_sec=duration_sec,
            trigger_fn=trigger_fn,
            trigger_delay_sec=trigger_delay_sec,
            decode_video=False,
            audio_only=True,
        )
        if not isinstance(res, dict):
            res = {}
        wav_exists = bool(output_wav_path and Path(output_wav_path).is_file() and Path(output_wav_path).stat().st_size > 44)
        wav_saved = bool(res.get("wav_saved", res.get("audio_ok", wav_exists)))
        audio_bytes = int(
            res.get("audio_bytes")
            or (max(0, Path(output_wav_path).stat().st_size - 44) if wav_exists else 0)
        )
        m_win = res.get("motor_window_sec")
        motor_dur = float(
            res.get("motor_duration_sec")
            or (round(max(0.0, float(m_win[1]) - float(m_win[0])), 2) if isinstance(m_win, (list, tuple)) and len(m_win) == 2 else 0.0)
        )
        res.setdefault("wav_saved", wav_saved)
        res.setdefault("audio_ok", wav_saved)
        res.setdefault("audio_bytes", audio_bytes)
        res.setdefault("motor_duration_sec", motor_dur)
        res.setdefault("trigger_ok", True if trigger_fn is None else not bool(res.get("error")))
        res.setdefault("trigger_detail", "")
        res["audio_only"] = True
        return res

    _SESSION_LOCK = threading.Lock()
    SESSION_LOCK_WAIT_SEC = 120.0

    def _run_session(self, coro: Any) -> Dict[str, Any]:
        if not CameraMixin._SESSION_LOCK.acquire(timeout=self.SESSION_LOCK_WAIT_SEC):
            coro.close()
            raise RuntimeError("Camera is busy with another session (timed out waiting)")
        try:
            return asyncio.run(coro)
        finally:
            CameraMixin._SESSION_LOCK.release()

    async def _imm_session_async(
        self,
        duration_sec: float = 0.0,
        snapshot_only_path: Optional[str] = None,
        output_mp4: Optional[str] = None,
        output_wav: Optional[str] = None,
        before_jpg: Optional[str] = None,
        after_jpg: Optional[str] = None,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 1.2,
        speaker_alaw_bytes: Optional[bytes] = None,
        decode_video: bool = True,
        audio_only: bool = False,
        timeout_sec: float = 20.0,
    ) -> Dict[str, Any]:
        if stun is None:
            raise RuntimeError("aioice is required for live camera sessions")
        loop = asyncio.get_running_loop()
        if audio_only:
            decode_video = False

        rtc_res = self.call_api(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {"devId": self.device_id, "type": "ipc"},
            allow_relogin=True,
        )
        if not rtc_res.get("success"):
            raise RuntimeError(f"Failed to fetch RTC config: {rtc_res}")

        rtc_cfg = rtc_res["result"]
        device_password = rtc_cfg.get("password", "")
        rtc_auth = rtc_cfg.get("auth", "")
        p2p_cfg = rtc_cfg.get("p2pConfig", {})
        moto_id = rtc_cfg.get("motoId") or p2p_cfg.get("motoId")
        ices = p2p_cfg.get("ices", [])

        turn_host, turn_port = None, 3478
        for ice in ices:
            urls = ice.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            for u in urls:
                if u.startswith("turn:") or u.startswith("stun:"):
                    hp = u.split(":", 1)[1].split("?")[0]
                    if ":" in hp:
                        turn_host, p_str = hp.split(":", 1)
                        turn_port = int(p_str)
                    else:
                        turn_host = hp
                    break
            if turn_host:
                break

        local_ufrag = _imm_rand_ascii(4)
        local_pwd = _imm_rand_ascii(24)
        local_aes_ascii = _imm_rand_ascii(16)
        now = int(time.time())
        session_id = f"{self.device_id}{now}{_imm_rand_ascii(8)}"
        trace_id = f"{str(uuid.uuid4()).upper()}_{self.device_id}_{now * 1000}"

        udp_proto = _ImmSessionProtocol(
            local_ufrag,
            local_pwd,
            decode_video=decode_video,
            cloud_only=(self.mode == "cloud"),
            audio_only=audio_only,
        )
        udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: udp_proto, local_addr=("0.0.0.0", 0)
        )

        if turn_host:
            udp_proto.stun_server_addr = (turn_host, turn_port)
            stun_req = stun.Message(
                message_method=stun.Method.BINDING,
                message_class=stun.Class.REQUEST,
            )
            udp_transport.sendto(bytes(stun_req), (turn_host, turn_port))
            await asyncio.sleep(0.35)

        pub_topic = f"/av/moto/{moto_id}/u/{self.device_id}"
        sub_topic = f"/av/u/{self.uid}"
        mqtt_connected = asyncio.Event()
        answer_event = asyncio.Event()
        cam_candidates: List[Tuple[str, str, int]] = []

        def on_connect(c: Any, userdata: Any, flags: Any, rc: int, *args: Any) -> None:
            if rc == 0:
                c.subscribe(sub_topic, qos=1)
                loop.call_soon_threadsafe(mqtt_connected.set)

        def on_message(c: Any, userdata: Any, msg: Any) -> None:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                return
            if payload.get("protocol") != 302:
                return
            data = payload.get("data", {})
            hdr = data.get("header", {})
            if hdr.get("sessionid") and hdr.get("sessionid") != session_id:
                return
            mtype = hdr.get("type")
            mbody = data.get("msg", {})
            if isinstance(mbody, str):
                try:
                    mbody = json.loads(mbody)
                except Exception:
                    return

            if mtype == "answer":
                ans_sdp = mbody.get("sdp", "")
                for line in ans_sdp.splitlines():
                    line = line.strip()
                    if line.startswith("a=ice-ufrag:"):
                        udp_proto.remote_ufrag = line.split(":", 1)[1]
                    elif line.startswith("a=ice-pwd:"):
                        udp_proto.remote_pwd = line.split(":", 1)[1]
                    elif line.startswith("a=aes-key:"):
                        udp_proto.aes_key = bytes.fromhex(line.split(":", 1)[1])
                loop.call_soon_threadsafe(answer_event.set)
            elif mtype == "candidate":
                cand = mbody.get("candidate", "").strip()
                parts = cand.replace("a=", "").split()
                if len(parts) >= 8 and parts[6] == "typ":
                    cam_candidates.append((parts[7], parts[4], int(parts[5])))

        mclient = self._create_mqtt_client()
        mclient.on_connect = on_connect
        mclient.on_message = on_message
        mclient.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        mclient.loop_start()

        def pub_302(mtype: str, msg_obj: Dict[str, Any]) -> None:
            pkt = {
                "protocol": 302,
                "pv": "2.2",
                "t": int(time.time() * 1000),
                "data": {
                    "header": {
                        "from": self.uid,
                        "to": self.device_id,
                        "sub_dev_id": "",
                        "moto_id": moto_id,
                        "path": "mqtt",
                        "type": mtype,
                        "is_pre": 0,
                        "p2p_skill": 1635,
                        "security_level": 3,
                        "sessionid": session_id,
                        "trace_id": trace_id,
                        "tid": "",
                        "seq": 0,
                        "rtx": 0,
                    },
                    "msg": msg_obj,
                },
            }
            mclient.publish(pub_topic, json.dumps(pkt), qos=1)

        retransmit_task: Optional[asyncio.Task[Any]] = None
        try:
            await asyncio.wait_for(mqtt_connected.wait(), timeout=8.0)
            await asyncio.sleep(0.4)

            local_aes_hex = local_aes_ascii.encode("ascii").hex()
            offer_sdp = "\r\n".join(
                [
                    "v=0",
                    f"o=- {now} 1 IN IP4 127.0.0.1",
                    "s=-",
                    "t=0 0",
                    "a=group:BUNDLE imm0",
                    f"a=msid-semantic: WMS {session_id}",
                    "m=application 9 imm 6001",
                    "c=IN IP4 0.0.0.0",
                    "a=rtcp:9 IN IP4 0.0.0.0",
                    f"a=ice-ufrag:{local_ufrag}",
                    f"a=ice-pwd:{local_pwd}",
                    "a=ice-options:trickle",
                    f"a=aes-key:{local_aes_hex}",
                    "a=mid:imm0",
                    "a=rtpmap:6001 AES/KCP 330",
                    f"a=ssrc:0 cname:{self.uid}",
                    "",
                ]
            )

            pub_302(
                "offer",
                {
                    "sdp": offer_sdp,
                    "auth": rtc_auth,
                    "preconnect": False,
                    "token": ices,
                    "tcp_token": p2p_cfg.get("tcpRelay", {}),
                    "log": p2p_cfg.get("log", {}),
                },
            )

            if udp_proto.srflx_addr:
                srflx_ip, srflx_port = udp_proto.srflx_addr
                cand_str = (
                    f"a=candidate:1 1 UDP 1694498815 {srflx_ip} {srflx_port} "
                    "typ srflx raddr 0.0.0.0 rport 0\r\n"
                )
                pub_302("candidate", {"candidate": cand_str, "token": []})

            await asyncio.wait_for(answer_event.wait(), timeout=10.0)
            await asyncio.sleep(0.75)

            allowed_types = ("relay", "srflx") if self.mode == "cloud" else ("relay", "srflx", "host")
            cloud_cands = [c for c in cam_candidates if c[0] in allowed_types]
            if not cloud_cands:
                cloud_cands = list(cam_candidates)

            username = f"{udp_proto.remote_ufrag}:{local_ufrag}"
            for _ in range(25):
                if udp_proto.ice_connected.is_set():
                    break
                for _, cip, cport in cloud_cands:
                    req = stun.Message(
                        message_method=stun.Method.BINDING,
                        message_class=stun.Class.REQUEST,
                    )
                    req.attributes["USERNAME"] = username
                    req.attributes["PRIORITY"] = 2130706431
                    req.attributes["ICE-CONTROLLED"] = int.from_bytes(os.urandom(8), "big")
                    req.add_message_integrity(udp_proto.remote_pwd.encode("utf-8"))
                    udp_transport.sendto(bytes(req), (cip, cport))
                await asyncio.sleep(0.2)

            await asyncio.wait_for(udp_proto.ice_connected.wait(), timeout=8.0)

            async def _retransmit_kcp() -> None:
                while True:
                    await asyncio.sleep(0.12)
                    now_mono = time.monotonic()
                    now_ms = int(time.time() * 1000) & 0xFFFFFFFF
                    for conv_id in list(udp_proto.unacked.keys()):
                        unacked_map = udp_proto.unacked.get(conv_id)
                        ts_map = udp_proto.snd_ts.get(conv_id, {})
                        if unacked_map:
                            for sn, payload in list(unacked_map.items()):
                                if now_mono - ts_map.get(sn, 0.0) >= 0.18:
                                    ts_map[sn] = now_mono
                                    udp_proto.send_kcp_seg(conv_id, 81, 0, now_ms, sn, payload)

            retransmit_task = asyncio.create_task(_retransmit_kcp())

            tok = _imm_login_token(device_password, self.local_key)
            udp_proto.send_kcp_msg(0, _imm_seal_aes(udp_proto.aes_key, _imm_build_login(tok)))
            await asyncio.sleep(0.15)

            if speaker_alaw_bytes:
                return await self._imm_talk(udp_proto, speaker_alaw_bytes, output_wav)

            for raw in udp_proto.startup_messages():
                if len(raw) >= 16:
                    udp_proto.sent_ctrl_cmds.append(struct.unpack_from("<I", raw, 12)[0])
                udp_proto.send_kcp_msg(0, _imm_seal_aes(udp_proto.aes_key, raw))
                await asyncio.sleep(0.10)

            if audio_only or not decode_video:
                try:
                    await asyncio.wait_for(udp_proto.audio_ready.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.wait_for(udp_proto.any_video_ready.wait(), timeout=10.0)
                try:
                    await asyncio.wait_for(udp_proto.hd_video_ready.wait(), timeout=2.5)
                except asyncio.TimeoutError:
                    pass

            if snapshot_only_path and udp_proto.latest_rgb is not None and Image is not None:
                rgb = udp_proto.latest_rgb
                Image.fromarray(rgb).save(snapshot_only_path, quality=92)
                return {
                    "snapshot_ok": Path(snapshot_only_path).exists(),
                    "video_frames": len(udp_proto.video_frames),
                    "resolution": f"{rgb.shape[1]}x{rgb.shape[0]}",
                }

            before_ok = False
            if before_jpg and not audio_only and udp_proto.latest_rgb is not None and Image is not None:
                rgb = udp_proto.latest_rgb
                Image.fromarray(rgb).save(before_jpg, quality=92)
                before_ok = Path(before_jpg).exists()

            udp_proto.video_frames.clear()
            udp_proto.audio_alaw_chunks.clear()
            udp_proto.audio_pcm_chunks.clear()
            udp_proto.audio_rx_times.clear()
            udp_proto.recording_active = True
            rec_start = time.monotonic()

            if trigger_fn is not None:
                await asyncio.sleep(max(0.1, trigger_delay_sec))
                await loop.run_in_executor(None, trigger_fn)

            if duration_sec > 0:
                await asyncio.sleep(duration_sec)

            extended = 0.0
            max_extend = float(os.getenv("RECORD_MAX_EXTEND_SECONDS", "60"))
            while duration_sec > 0 and extended < max_extend and _motor_active_recently(
                udp_proto.audio_pcm_chunks, window_chunks=30
            ):
                await asyncio.sleep(1.0)
                extended += 1.0
            if extended:
                await asyncio.sleep(2.0)

            udp_proto.recording_active = False
            coverage = _recording_coverage(
                udp_proto.audio_pcm_chunks, udp_proto.audio_rate, udp_proto.audio_rx_times,
                rec_start, time.monotonic(),
            )
            frames = udp_proto.video_frames
            coverage["video_last_rx_sec"] = round(frames[-1][0] - rec_start, 2) if frames else None
            motor_win = _motor_window(udp_proto.audio_pcm_chunks, udp_proto.audio_rx_times)

            if before_jpg and not audio_only and motor_win and motor_win[0] - 0.4 > rec_start:
                if _save_frame_near(udp_proto.video_frames, motor_win[0] - 0.4, before_jpg, prefer="before"):
                    before_ok = True

            after_ok = False
            if after_jpg and not audio_only:
                if motor_win and _save_frame_near(udp_proto.video_frames, motor_win[1] + 2.5, after_jpg, prefer="after"):
                    after_ok = True
                elif udp_proto.latest_rgb is not None and Image is not None:
                    Image.fromarray(udp_proto.latest_rgb).save(after_jpg, quality=92)
                    after_ok = Path(after_jpg).exists()

            audio_ok = False
            if output_wav:
                if udp_proto.audio_pcm_chunks:
                    audio_ok = self._write_audio_wav(
                        udp_proto.audio_pcm_chunks, udp_proto.audio_rate, output_wav
                    )
                elif udp_proto.audio_alaw_chunks:
                    audio_ok = write_wav_pure_python(
                        output_wav, udp_proto.audio_alaw_chunks, sample_rate=udp_proto.audio_rate, is_alaw=True
                    )

            video_ok = False
            if output_mp4 and not audio_only and decode_video and udp_proto.video_frames:
                video_ok = await loop.run_in_executor(
                    None,
                    self._encode_frames_to_mp4,
                    udp_proto.video_frames,
                    output_mp4,
                    output_wav if audio_ok else None,
                )

            audio_bytes = sum(len(c) for c in udp_proto.audio_alaw_chunks) * 2
            if not audio_bytes and udp_proto.audio_pcm_chunks:
                audio_bytes = sum(len(c) * 2 for c in udp_proto.audio_pcm_chunks)
            motor_dur = round(max(0.0, motor_win[1] - motor_win[0]), 2) if motor_win else 0.0

            return {
                "video_ok": video_ok,
                "audio_ok": audio_ok,
                "wav_saved": audio_ok,
                "mp4_saved": video_ok,
                "before_ok": before_ok,
                "after_ok": after_ok,
                "video_frames": len(udp_proto.video_frames),
                "audio_frames": len(udp_proto.audio_pcm_chunks),
                "audio_bytes": audio_bytes,
                "audio_rate": udp_proto.audio_rate,
                "audio_only": bool(audio_only),
                "recording_extended_sec": extended,
                "motor_duration_sec": motor_dur,
                "motor_window_sec": (
                    [round(motor_win[0] - rec_start, 2), round(motor_win[1] - rec_start, 2)] if motor_win else None
                ),
                **coverage,
            }
        finally:
            if retransmit_task is not None:
                retransmit_task.cancel()
            try:
                pub_302("disconnect", {})
            except Exception:
                pass
            try:
                udp_transport.close()
            except Exception:
                pass
            mclient.loop_stop()
            mclient.disconnect()


CameraStreamMixin = CameraMixin
