"""Unified Molly & Cody Smart Feeder client (`MollyFeederClient`)."""
from __future__ import annotations

from custom_components.molly_cody_feeder.core.molly.camera import CameraMixin
from custom_components.molly_cody_feeder.core.molly.cloud import CloudAuthMixin
from custom_components.molly_cody_feeder.core.molly.device import DeviceDpsMixin
from custom_components.molly_cody_feeder.core.molly.media import MediaEncodingMixin
from custom_components.molly_cody_feeder.core.molly.speaker import SpeakerMixin
from custom_components.molly_cody_feeder.core.molly.webrtc import WebRtcMixin


class MollyFeederClient(
    DeviceDpsMixin,
    CloudAuthMixin,
    CameraMixin,
    SpeakerMixin,
    MediaEncodingMixin,
    WebRtcMixin,
):
    """Unified Cloud + Local LAN client for the Molly & Cody Smart Feeder."""
