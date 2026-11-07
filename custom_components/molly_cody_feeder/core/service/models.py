"""Request models for feed verification (`VerifyRequest`)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VerifyRequest:
    trigger_dispense: bool = False
    portions: int = 1
    expect_dispense: bool = True
    event_type: str = ""

    @property
    def label(self) -> str:
        if self.event_type:
            return self.event_type
        if self.trigger_dispense:
            return "manual_dispense_verify"
        return "scheduled_verify" if self.expect_dispense else "manual_check"
