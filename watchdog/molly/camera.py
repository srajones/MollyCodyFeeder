"""Camera sessions over the cloud `imm` protocol: snapshots and audio/video verification clips."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PIL import Image
from aioice import stun

from watchdog.molly.constants import MQTT_HOST, MQTT_PORT
from watchdog.molly.imm_codec import (
    _IMM_STARTUP_MESSAGES,
    _imm_build_login,
    _imm_login_token,
    _imm_rand_ascii,
    _imm_seal_aes,
)
from watchdog.molly.imm_transport import _ImmSessionProtocol
from watchdog.molly.media import _motor_active_recently, _motor_window, _save_frame_near

logger = logging.getLogger("poor_puppy.molly.camera")


def _recording_coverage(
    pcm_chunks: List[Any], audio_rate: int, rx_times: List[float], rec_start: float, rec_stop: float
) -> Dict[str, Any]:
    """Recording coverage (pure): wall-clock length of the recording window vs. audio actually received.

    - recorded_wall_sec: monotonic seconds from the start of recording to the stop
    - audio_sec: seconds of PCM audio received (total samples / sample rate)
    - audio_last_rx_sec: arrival time of the last audio chunk relative to the start (None = no audio)
    """
    samples = sum(len(c) for c in pcm_chunks)
    rate = float(audio_rate or 8000)
    return {
        "recorded_wall_sec": round(max(0.0, rec_stop - rec_start), 2),
        "audio_sec": round(samples / rate, 2),
        "audio_last_rx_sec": round(rx_times[-1] - rec_start, 2) if rx_times else None,
    }


class CameraMixin:
    """Snapshots and recorded verification clips (one camera session at a time)."""

    # -------------------------------------------------------------------------
    # Live 1280x720 HD Cloud `imm` (AES/KCP 330) Stream & Two-Way Speaker TTS
    # -------------------------------------------------------------------------
    def capture_snapshot(self, output_path: str, timeout_sec: float = 15.0) -> bool:
        """Captures an upright 720x1280 HD JPEG frame from the feeder's camera over Cloud `imm`."""
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
        duration_sec: float,
        output_mp4: str,
        output_wav: str,
        before_jpg: Optional[str] = None,
        after_jpg: Optional[str] = None,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 1.2,
    ) -> Dict[str, Any]:
        """
        Opens a 100% Cloud `imm` (AES/KCP 330) HD video + audio session and records:
        - Optional `before_jpg` once 720x1280 upright HD frame stabilizes
        - Optional `trigger_fn()` callback (e.g. dispensing food while live stream is active!)
        - `output_mp4` (H.264 720x1280 upright video + AAC audio) and `output_wav` (8000 Hz mono WAV audio)
        - Optional `after_jpg` at the end of the recording window
        """
        Path(output_mp4).parent.mkdir(parents=True, exist_ok=True)
        Path(output_wav).parent.mkdir(parents=True, exist_ok=True)
        if before_jpg:
            Path(before_jpg).parent.mkdir(parents=True, exist_ok=True)
        if after_jpg:
            Path(after_jpg).parent.mkdir(parents=True, exist_ok=True)

        fired = {"v": False}

        def _guarded_trigger():
            fired["v"] = True
            return trigger_fn()

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                return self._run_session(
                    self._imm_session_async(
                        duration_sec=duration_sec,
                        output_mp4=output_mp4,
                        output_wav=output_wav,
                        before_jpg=before_jpg,
                        after_jpg=after_jpg,
                        trigger_fn=_guarded_trigger if trigger_fn else None,
                        trigger_delay_sec=trigger_delay_sec,
                        timeout_sec=duration_sec + 22.0,
                    )
                )
            except Exception as exc:
                last_exc = exc
                # Session setup (signaling / ICE) occasionally fails on the first try. Retry once,
                # but NEVER after the dispense command was sent (that would feed twice).
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
                # A camera problem must never block feeding: dispense anyway, just unverified
                logger.warning("Camera unavailable: dispensing WITHOUT video verification")
                try:
                    _guarded_trigger()
                    dispensed_unverified = True
                except Exception as trig_exc:
                    logger.error("Fallback dispense failed: %s", trig_exc)
            return {
                "video_ok": False,
                "audio_ok": False,
                "before_ok": False,
                "after_ok": False,
                "video_frames": 0,
                "audio_frames": 0,
                "error": str(exc),
                "dispensed_unverified": dispensed_unverified,
            }

    # One camera session at a time per process: the watchdog schedule, webhook threads and the
    # review server would otherwise open overlapping imm sessions that starve each other.
    _SESSION_LOCK = threading.Lock()

    SESSION_LOCK_WAIT_SEC = 120.0

    def _run_session(self, coro):
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
        timeout_sec: float = 20.0,
    ) -> Dict[str, Any]:
        """
        Establishes a 100% Cloud `imm` (`AES/KCP 330`) session with the Molly & Cody Smart Feeder:
        - Connects over Cloud STUN/TURN relay (`15.204.x.x`) without needing LAN access
        - Streams simultaneous 1280x720@15fps H.264 video (`conv=1`, rotated 90° CW to 720x1280)
          and 8000 Hz G.711 A-law (`PCMA`) microphone audio (`conv=2`)
        - Optionally streams Two-Way Speaker audio (`speaker_alaw_bytes`) on `conv=0` + `conv=2`
        """
        loop = asyncio.get_running_loop()

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

        def on_connect(c, userdata, flags, rc, *args):
            if rc == 0:
                c.subscribe(sub_topic, qos=1)
                loop.call_soon_threadsafe(mqtt_connected.set)

        def on_message(c, userdata, msg):
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

        def pub_302(mtype: str, msg_obj: Dict[str, Any]):
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

        retransmit_task: Optional[asyncio.Task] = None
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

            async def _retransmit_kcp():
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

            # Authenticate on conv=0
            tok = _imm_login_token(device_password, self.local_key)
            udp_proto.send_kcp_msg(0, _imm_seal_aes(udp_proto.aes_key, _imm_build_login(tok)))
            await asyncio.sleep(0.15)

            # Two-Way Speaker Mode (stream `speaker_alaw_bytes` to feeder's built-in speaker)
            if speaker_alaw_bytes:
                return await self._imm_talk(udp_proto, speaker_alaw_bytes, output_wav)

            for raw in _IMM_STARTUP_MESSAGES:
                udp_proto.send_kcp_msg(0, _imm_seal_aes(udp_proto.aes_key, raw))
                await asyncio.sleep(0.10)

            # Wait for first decoded video frame and HD 720x1280 stabilization
            await asyncio.wait_for(udp_proto.any_video_ready.wait(), timeout=10.0)
            try:
                await asyncio.wait_for(udp_proto.hd_video_ready.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                pass

            if snapshot_only_path and udp_proto.latest_rgb is not None:
                rgb = udp_proto.latest_rgb
                Image.fromarray(rgb).save(snapshot_only_path, quality=92)
                return {
                    "snapshot_ok": Path(snapshot_only_path).exists(),
                    "video_frames": len(udp_proto.video_frames),
                    "resolution": f"{rgb.shape[1]}x{rgb.shape[0]}",
                }

            before_ok = False
            if before_jpg and udp_proto.latest_rgb is not None:
                rgb = udp_proto.latest_rgb
                Image.fromarray(rgb).save(before_jpg, quality=92)
                before_ok = Path(before_jpg).exists()
                logger.info("Saved BEFORE HD snapshot to %s (%dx%d)", before_jpg, rgb.shape[1], rgb.shape[0])

            # Reset buffers so recorded MP4 and WAV start right at the dispense window
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

            # Dispense start is not deterministic (cloud/MCU delays) and run time scales with
            # portions, so keep recording while the motor is still audibly running (bounded).
            extended = 0.0
            max_extend = float(os.getenv("RECORD_MAX_EXTEND_SECONDS", "60"))
            while duration_sec > 0 and extended < max_extend and _motor_active_recently(
                udp_proto.audio_pcm_chunks, window_chunks=30
            ):
                await asyncio.sleep(1.0)
                extended += 1.0
            if extended:
                # small tail so the last pellets settle before the AFTER frame
                await asyncio.sleep(2.0)
                logger.info("Recording auto-extended by %.0fs while the motor was running", extended + 2.0)

            udp_proto.recording_active = False
            # How much of the planned window actually arrived: the cloud stream sometimes stops
            # delivering audio/video a few seconds in. A short WAV is a capture failure, not a clog.
            coverage = _recording_coverage(
                udp_proto.audio_pcm_chunks, udp_proto.audio_rate, udp_proto.audio_rx_times,
                rec_start, time.monotonic(),
            )
            if coverage["audio_sec"] < 0.75 * coverage["recorded_wall_sec"]:
                logger.warning(
                    "Camera stream delivered only %.1fs of audio in a %.1fs recording (last audio at %s s)",
                    coverage["audio_sec"], coverage["recorded_wall_sec"], coverage["audio_last_rx_sec"],
                )
            # Video can keep arriving after the audio drops out (seen on real feeds): record how much
            frames = udp_proto.video_frames
            coverage["video_last_rx_sec"] = round(frames[-1][0] - rec_start, 2) if frames else None
            motor_win = _motor_window(udp_proto.audio_pcm_chunks, udp_proto.audio_rx_times)

            # BEFORE frame: just before the detected motor onset (robust to dispense delay)
            if before_jpg and motor_win and motor_win[0] - 0.4 > rec_start:
                if _save_frame_near(udp_proto.video_frames, motor_win[0] - 0.4, before_jpg, prefer="before"):
                    before_ok = True
                    logger.info("BEFORE frame re-picked at motor onset -%.1fs", 0.4)

            after_ok = False
            if after_jpg:
                # AFTER frame: ~2.5 s after the motor stops (pellets settled), else the latest frame
                if motor_win and _save_frame_near(udp_proto.video_frames, motor_win[1] + 2.5, after_jpg, prefer="after"):
                    after_ok = True
                elif udp_proto.latest_rgb is not None:
                    Image.fromarray(udp_proto.latest_rgb).save(after_jpg, quality=92)
                    after_ok = Path(after_jpg).exists()
                if after_ok:
                    logger.info("Saved AFTER HD snapshot to %s", after_jpg)

            audio_ok = False
            if output_wav and udp_proto.audio_pcm_chunks:
                audio_ok = self._write_audio_wav(
                    udp_proto.audio_pcm_chunks, udp_proto.audio_rate, output_wav
                )

            video_ok = False
            if output_mp4 and udp_proto.video_frames:
                video_ok = await loop.run_in_executor(
                    None,
                    self._encode_frames_to_mp4,
                    udp_proto.video_frames,
                    output_mp4,
                    output_wav if audio_ok else None,
                )

            return {
                "video_ok": video_ok,
                "audio_ok": audio_ok,
                "before_ok": before_ok,
                "after_ok": after_ok,
                "video_frames": len(udp_proto.video_frames),
                "audio_frames": len(udp_proto.audio_pcm_chunks),
                "audio_rate": udp_proto.audio_rate,
                "recording_extended_sec": extended,
                "motor_window_sec": (
                    [round(motor_win[0] - rec_start, 2), round(motor_win[1] - rec_start, 2)] if motor_win else None
                ),
                **coverage,  # recorded_wall_sec, audio_sec, audio_last_rx_sec
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
