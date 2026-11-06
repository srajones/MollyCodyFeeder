"""Backward-compatible import path for the feeder client.

The implementation lives in the `watchdog.molly` package (small single-purpose modules).
New code should use `from watchdog.molly import MollyFeederClient`. This shim re-exports every
public and helper name that used to live here so existing scripts and tests keep working.
"""
from types import ModuleType

from watchdog.molly import (  # noqa: F401
    camera,
    client,
    cloud,
    constants,
    crypto,
    device,
    imm_codec,
    imm_transport,
    media,
    net,
    speaker,
    webrtc,
    webrtc_compat,
)
from watchdog.molly.client import MollyFeederClient  # noqa: F401

for _mod in (constants, crypto, imm_codec, imm_transport, media, net, webrtc_compat, cloud, device, camera,
             speaker, webrtc, client):
    for _name, _val in vars(_mod).items():
        if not _name.startswith("__") and not isinstance(_val, ModuleType):
            globals().setdefault(_name, _val)
del _mod, _name, _val
