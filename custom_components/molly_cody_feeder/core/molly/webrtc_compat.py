"""aiortc compatibility shims for the legacy WebRTC fallback path."""
from __future__ import annotations

import asyncio
from typing import Any

try:
    from aiortc import MediaStreamTrack  # type: ignore[import-not-found]
    from aiortc.rtcdtlstransport import RTCDtlsTransport, State  # type: ignore[import-not-found]

    _HAS_AIORTC = True
except ImportError:
    _HAS_AIORTC = False

    class MediaStreamTrack:  # type: ignore[no-redef]
        kind = "audio"

        def __init__(self) -> None:
            pass


def _safe_validate_peer_identity(self: Any, remoteParameters: Any) -> None:
    pycert = self._ssl.get_peer_certificate(as_cryptography=False)
    if pycert is None:
        self._set_state(State.FAILED)
        return
    fingerprint_supported = 0
    fingerprint_valid = 0
    for f in remoteParameters.fingerprints:
        algorithm = f.algorithm.lower().replace("-", "")
        try:
            digest = pycert.digest(algorithm).decode("ascii").upper()
            fingerprint_supported += 1
            if f.value.upper() == digest:
                fingerprint_valid += 1
        except Exception:
            pass
    if not fingerprint_supported or fingerprint_valid != fingerprint_supported:
        self._set_state(State.FAILED)


class _PassiveAudioTrack(MediaStreamTrack):
    """Registers a=sendrecv on the audio m-line without blasting talkback RTP."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = asyncio.Event()

    async def recv(self) -> Any:
        await self._stop_event.wait()
        raise asyncio.CancelledError()


if _HAS_AIORTC:
    RTCDtlsTransport._validate_peer_identity = _safe_validate_peer_identity
