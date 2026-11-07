"""Retry helper: distinguishes 'request never left the box' from 'server may have acted on it'."""
from __future__ import annotations

import socket
import time
from typing import Callable, Tuple, TypeVar

import requests

T = TypeVar("T")

_NEVER_SENT_EXC: Tuple[type, ...] = (
    requests.exceptions.ConnectTimeout,
    socket.gaierror,
)


def _never_sent(exc: BaseException) -> bool:
    if isinstance(exc, _NEVER_SENT_EXC):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError) and not isinstance(
        exc, requests.exceptions.ReadTimeout
    ):
        msg = str(exc)
        return any(k in msg for k in ("NewConnectionError", "NameResolutionError", "Failed to establish"))
    return False


def _retry_if_unreached(
    fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.6
) -> T:
    last_exc: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i + 1 >= attempts or not _never_sent(exc):
                raise
            time.sleep(base_delay * (2 ** i))
    assert last_exc is not None
    raise last_exc
