"""Plain data objects passed between the verification steps (no I/O, easy to serialize for a future web UI)."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class VerifyRequest:
    """What to verify. `expect_dispense` defaults to `trigger_dispense` (on-demand checks expect none)."""

    trigger_dispense: bool = False
    portions: int = 1
    announce_clog_tts: bool = True
    event_type: Optional[str] = None
    record_seconds: Optional[float] = None
    expected_portions: int = 0
    expect_dispense: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.expect_dispense is None:
            self.expect_dispense = self.trigger_dispense

    @property
    def meal_portions(self) -> int:
        return self.portions if self.trigger_dispense else int(self.expected_portions or 0)

    @property
    def db_event_type(self) -> str:
        if self.event_type:
            return self.event_type
        if self.trigger_dispense:
            return "manual_dispense_verify"
        return "scheduled_verify" if self.expect_dispense else "bowl_check"


@dataclass
class CaptureFiles:
    """Paths of every artifact produced by one verification run."""

    before: str
    after: str
    annotated: str
    video: str
    audio: str

    @classmethod
    def for_run(cls, work_dir: Path) -> "CaptureFiles":
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return cls(
            before=str(work_dir / f"{ts}_before.jpg"),
            after=str(work_dir / f"{ts}_after.jpg"),
            annotated=str(work_dir / f"{ts}_annotated.jpg"),
            video=str(work_dir / f"{ts}_dispense.mp4"),
            audio=str(work_dir / f"{ts}_dispense.wav"),
        )

    def existing(self, name: str) -> Optional[str]:
        path = getattr(self, name)
        return path if Path(path).exists() else None


@dataclass
class CaptureResult:
    """Raw outcome of the capture step (camera + telemetry), before any analysis."""

    pre_status: Dict[str, Any]
    post_status: Dict[str, Any]
    audio_ok: bool
    snapshot_ok: bool
    dispense_ok: bool = False
    dispense_transport: str = "none"
    session: Dict[str, Any] = field(default_factory=dict)

    @property
    def feed_abnormal(self) -> int:
        return int(self.post_status.get("feed_abnormal") or 0)
