"""Feed verification, retention, scheduling, maintenance, and summary subpackage."""
from __future__ import annotations

from custom_components.molly_cody_feeder.core.service.decision import (
    BOWL_CHECK,
    CAMERA_UNAVAILABLE,
    DISPENSED,
    MOTOR_NO_KIBBLE,
    NO_MOTOR,
    RECORDING_CUT_SHORT,
    UNVERIFIED,
    classify,
    decide_passed,
    is_clog,
    is_unverified,
    recording_cut_short,
    status_label,
)
from custom_components.molly_cody_feeder.core.service.maintenance import MaintenanceMixin
from custom_components.molly_cody_feeder.core.service.models import VerifyRequest
from custom_components.molly_cody_feeder.core.service.retention import RetentionManager
from custom_components.molly_cody_feeder.core.service.scheduling import SchedulingMixin
from custom_components.molly_cody_feeder.core.service.summary import (
    compute_ha_summary,
    meal_result,
    meal_timing_from_schedule,
)
from custom_components.molly_cody_feeder.core.service.verifier import FeedVerifier

__all__ = [
    "DISPENSED",
    "MOTOR_NO_KIBBLE",
    "NO_MOTOR",
    "UNVERIFIED",
    "CAMERA_UNAVAILABLE",
    "RECORDING_CUT_SHORT",
    "BOWL_CHECK",
    "recording_cut_short",
    "decide_passed",
    "classify",
    "is_clog",
    "is_unverified",
    "status_label",
    "VerifyRequest",
    "FeedVerifier",
    "RetentionManager",
    "MaintenanceMixin",
    "SchedulingMixin",
    "compute_ha_summary",
    "meal_result",
    "meal_timing_from_schedule",
]
