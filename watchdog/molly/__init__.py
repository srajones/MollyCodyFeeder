"""Molly & Cody smart feeder client package.

Layout (one responsibility per module):
    constants      OEM cloud constants, DP name table
    crypto         request signing / password + MQTT 2.2 encryption
    cloud          CloudSessionMixin   – session persistence, signed API, self-healing login
    device         DeviceControlMixin  – DPs, status, dispensing, schedule
    imm_codec      camera protocol framing / crypto helpers (pure functions)
    imm_transport  UDP/KCP session transport + media demux
    camera         CameraMixin         – snapshots, verification clips
    speaker        SpeakerMixin        – TTS / audio playback through the feeder speaker
    media          motor-window detection, frame selection, MP4/WAV writers
    webrtc(_compat) legacy WebRTC fallback
    client         MollyFeederClient facade composing the mixins
"""
from watchdog.molly.client import MollyFeederClient

__all__ = ["MollyFeederClient"]
