"""aiortc compatibility shims for the legacy WebRTC fallback path."""

import asyncio

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


# -----------------------------------------------------------------------------
# Patch aiortc DTLS peer certificate fingerprint validation to use OpenSSL
# directly (avoids cryptography>=42 strict ASN.1 parser error on Tuya camera certs)
# -----------------------------------------------------------------------------
def _safe_validate_peer_identity(self, remoteParameters) -> None:
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

    def __init__(self):
        super().__init__()
        self._stop_event = asyncio.Event()

    async def recv(self):
        await self._stop_event.wait()
        raise asyncio.CancelledError()


if _HAS_AIORTC:
    # Patch aiortc DTLS fingerprint validation (cryptography>=42 rejects the camera certificate)
    RTCDtlsTransport._validate_peer_identity = _safe_validate_peer_identity
