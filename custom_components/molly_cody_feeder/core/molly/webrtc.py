"""Legacy WebRTC camera session (fallback when the native `imm` session fails)."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from custom_components.molly_cody_feeder.core.molly.constants import (
    MQTT_HOST,
    MQTT_PORT,
)
from custom_components.molly_cody_feeder.core.molly.crypto import (
    _decrypt_mqtt_22,
)
from custom_components.molly_cody_feeder.core.molly.webrtc_compat import (
    _PassiveAudioTrack,
)

try:
    from aiortc import (  # type: ignore[import-not-found]
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCRtpSender,
        RTCSessionDescription,
    )

    _HAS_AIORTC = True
except ImportError:
    _HAS_AIORTC = False

logger = logging.getLogger("molly_cody_feeder.molly.webrtc")


class WebRtcMixin:
    """WebRTC fallback session."""

    async def _webrtc_session_async(
        self,
        duration_sec: float = 0.0,
        snapshot_only_path: Optional[str] = None,
        output_mp4: Optional[str] = None,
        output_wav: Optional[str] = None,
        before_jpg: Optional[str] = None,
        after_jpg: Optional[str] = None,
        trigger_fn: Optional[Callable[[], Any]] = None,
        trigger_delay_sec: float = 1.2,
        timeout_sec: float = 20.0,
    ) -> Dict[str, Any]:
        if not _HAS_AIORTC:
            raise RuntimeError("aiortc is not installed for WebRTC fallback")
        loop = asyncio.get_running_loop()

        rtc_res = self.call_api(
            "m.ipc.v4.rtc.config.get",
            "1.0",
            {"devId": self.device_id, "type": "ipc"},
            allow_relogin=True,
        )
        if not rtc_res.get("success"):
            raise RuntimeError(f"Failed to fetch WebRTC config: {rtc_res}")

        rtc_cfg = rtc_res["result"]
        p2p_cfg = rtc_cfg.get("p2pConfig", {})
        rtc_auth = rtc_cfg.get("auth", "")
        moto_id = rtc_cfg.get("motoId") or p2p_cfg.get("motoId")
        raw_ices = p2p_cfg.get("ices", [])
        session_id = "".join(random.choices(string.ascii_letters + string.digits, k=6))

        ice_servers = []
        for ice in raw_ices:
            urls = ice.get("urls", "")
            if "[" in urls:
                continue
            ice_servers.append(
                RTCIceServer(
                    urls=urls,
                    username=ice.get("username"),
                    credential=ice.get("credential"),
                )
            )

        sub_topic_av = f"/av/u/{self.uid}"
        pub_topic_av = f"/av/moto/{moto_id}/u/{self.device_id}"
        sub_topic_dp = f"smart/mb/out/{self.device_id}"

        mqtt_connected = asyncio.Event()
        answer_queue: asyncio.Queue[str] = asyncio.Queue()
        candidate_queue: asyncio.Queue[str] = asyncio.Queue()

        def on_connect(client: Any, userdata: Any, flags: Any, rc: int, *args: Any) -> None:
            if rc == 0:
                client.subscribe(sub_topic_av, qos=1)
                client.subscribe(sub_topic_dp, qos=1)
                loop.call_soon_threadsafe(mqtt_connected.set)

        def on_message(client: Any, userdata: Any, msg: Any) -> None:
            if msg.topic == sub_topic_dp:
                dec = _decrypt_mqtt_22(msg.payload, self.local_key)
                if dec and isinstance(dec.get("data", {}).get("dps"), dict):
                    rec_dps = {str(k): v for k, v in dec["data"]["dps"].items()}
                    self.cached_dps.update(rec_dps)
                    self._dp_ack_evt.set()
                return
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except Exception:
                return
            data = payload.get("data", {})
            hdr = data.get("header", {})
            if hdr.get("sessionid") != session_id:
                return
            mtype = hdr.get("type")
            mbody = data.get("msg", {})
            if mtype == "answer":
                loop.call_soon_threadsafe(answer_queue.put_nowait, mbody.get("sdp", ""))
            elif mtype == "candidate":
                cand = mbody.get("candidate", "")
                if cand:
                    loop.call_soon_threadsafe(candidate_queue.put_nowait, cand)

        mclient = self._create_mqtt_client()
        mclient.on_connect = on_connect
        mclient.on_message = on_message
        mclient.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        mclient.loop_start()
        self._active_mqtt_client = mclient

        def send_mqtt_frame(mtype: str, protocol: int, msg_obj: Dict[str, Any]) -> None:
            pkt = {
                "protocol": protocol,
                "pv": "2.2",
                "t": int(time.time()),
                "data": {
                    "header": {
                        "type": mtype,
                        "from": self.uid,
                        "to": self.device_id,
                        "sub_dev_id": "",
                        "sessionid": session_id,
                        "moto_id": moto_id,
                        "tid": "",
                    },
                    "msg": msg_obj,
                },
            }
            mclient.publish(pub_topic_av, json.dumps(pkt, separators=(",", ":")).encode("utf-8"), qos=1)

        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))
        audio_t = pc.addTransceiver(_PassiveAudioTrack(), direction="sendrecv")
        audio_codecs = [
            c
            for c in RTCRtpSender.getCapabilities("audio").codecs
            if c.mimeType in ("audio/PCMU", "audio/PCMA")
        ]
        if audio_codecs:
            audio_t.setCodecPreferences(audio_codecs)

        video_t = pc.addTransceiver("video", direction="recvonly")
        video_codecs = [
            c
            for c in RTCRtpSender.getCapabilities("video").codecs
            if c.mimeType == "video/H264"
        ]
        if video_codecs:
            video_t.setCodecPreferences(video_codecs)

        @pc.on("connectionstatechange")
        def on_conn_state() -> None:
            if pc.connectionState == "connected":
                send_mqtt_frame("resolution", 312, {"mode": "webrtc", "cmdValue": 0})
                send_mqtt_frame("audio", 312, {"mode": "webrtc", "cmdValue": 1})

        video_frames: List[Tuple[float, np.ndarray]] = []
        audio_pcm_chunks: List[np.ndarray] = []
        audio_sample_rate = 8000
        hd_frame_ready = asyncio.Event()
        any_frame_ready = asyncio.Event()
        stop_consumers = asyncio.Event()

        async def consume_video(track: Any) -> None:
            while not stop_consumers.is_set():
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                    rgb = frame.to_ndarray(format="rgb24")
                    rgb = np.ascontiguousarray(np.rot90(rgb, k=3))
                    video_frames.append((time.time(), rgb))
                    any_frame_ready.set()
                    if max(frame.width, frame.height) >= 1280 and len(video_frames) >= 8:
                        hd_frame_ready.set()
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

        async def consume_audio(track: Any) -> None:
            nonlocal audio_sample_rate
            while not stop_consumers.is_set():
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=2.0)
                    audio_sample_rate = frame.sample_rate or 8000
                    pcm = frame.to_ndarray()
                    audio_pcm_chunks.append(pcm.flatten())
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

        @pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind == "video":
                asyncio.create_task(consume_video(track))
            elif track.kind == "audio":
                asyncio.create_task(consume_audio(track))

        try:
            await asyncio.wait_for(mqtt_connected.wait(), timeout=8.0)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            audio_section = pc.localDescription.sdp.split("m=video")[0]
            local_cands = [
                line for line in audio_section.splitlines() if line.startswith("a=candidate:")
            ]
            offer_sdp = re.sub(r"\r\na=extmap[^\r\n]*", "", pc.localDescription.sdp)
            offer_sdp = re.sub(r"\r\na=candidate:[^\r\n]*", "", offer_sdp)

            send_mqtt_frame(
                "offer",
                302,
                {
                    "mode": "webrtc",
                    "sdp": offer_sdp,
                    "stream_type": 0,
                    "auth": rtc_auth,
                    "datachannel_enable": False,
                    "token": raw_ices,
                },
            )

            seen_cands = set()
            for lc in local_cands:
                ip_part = lc.split()[4]
                if ":" in ip_part:
                    continue
                if self.mode == "cloud" and (ip_part.startswith("192.168.") or ip_part.startswith("10.")):
                    continue
                if lc not in seen_cands:
                    seen_cands.add(lc)
                    send_mqtt_frame("candidate", 302, {"mode": "webrtc", "candidate": lc})

            answer_sdp = await asyncio.wait_for(answer_queue.get(), timeout=10.0)
            await asyncio.sleep(0.55)

            cands = []
            while not candidate_queue.empty():
                c = candidate_queue.get_nowait().strip()
                if c.startswith("a="):
                    c = c[2:]
                if self.mode == "cloud" and "typ host" in c:
                    continue
                if self.mode == "lan" and "typ host" not in c:
                    continue
                if ("typ srflx" in c or "typ relay" in c) and "raddr" not in c:
                    c = c + " raddr 0.0.0.0 rport 0"
                cands.append(f"a={c}\r\n")

            if cands:
                cand_block = "".join(cands)
                parts = answer_sdp.split("m=video ")
                if len(parts) == 2:
                    answer_sdp = parts[0] + cand_block + "m=video " + parts[1] + cand_block
                else:
                    answer_sdp = answer_sdp + cand_block

            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

            await asyncio.wait_for(any_frame_ready.wait(), timeout=12.0)
            try:
                await asyncio.wait_for(hd_frame_ready.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                pass

            if snapshot_only_path:
                _, latest_rgb = video_frames[-1]
                Image.fromarray(latest_rgb).save(snapshot_only_path, quality=92)
                return {
                    "snapshot_ok": Path(snapshot_only_path).exists(),
                    "video_frames": len(video_frames),
                    "resolution": f"{latest_rgb.shape[1]}x{latest_rgb.shape[0]}",
                }

            before_ok = False
            if before_jpg and video_frames:
                _, latest_rgb = video_frames[-1]
                Image.fromarray(latest_rgb).save(before_jpg, quality=92)
                before_ok = Path(before_jpg).exists()

            video_frames.clear()
            audio_pcm_chunks.clear()

            if trigger_fn is not None:
                await asyncio.sleep(max(0.1, trigger_delay_sec))
                await loop.run_in_executor(None, trigger_fn)

            if duration_sec > 0:
                await asyncio.sleep(duration_sec)

            stop_consumers.set()

            after_ok = False
            if after_jpg and video_frames:
                _, latest_rgb = video_frames[-1]
                Image.fromarray(latest_rgb).save(after_jpg, quality=92)
                after_ok = Path(after_jpg).exists()

            video_ok = False
            if output_mp4 and video_frames:
                video_ok = await loop.run_in_executor(
                    None, self._encode_frames_to_mp4, video_frames, output_mp4
                )

            audio_ok = False
            if output_wav and audio_pcm_chunks:
                audio_ok = self._write_audio_wav(audio_pcm_chunks, audio_sample_rate, output_wav)

            return {
                "video_ok": video_ok,
                "audio_ok": audio_ok,
                "before_ok": before_ok,
                "after_ok": after_ok,
                "video_frames": len(video_frames),
                "audio_frames": len(audio_pcm_chunks),
            }
        finally:
            self._active_mqtt_client = None
            stop_consumers.set()
            try:
                send_mqtt_frame("disconnect", 302, {"mode": "webrtc"})
            except Exception:
                pass
            try:
                await pc.close()
            except Exception:
                pass
            mclient.loop_stop()
            mclient.disconnect()
