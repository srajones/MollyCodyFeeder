"""Network error classification helpers."""

import requests


def _never_sent(exc: Exception) -> bool:
    """True only for failures that provably happened before the HTTP request was sent."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    text = repr(exc)
    return any(k in text for k in ("NewConnectionError", "NameResolutionError", "Failed to establish a new connection"))
