"""Pure decision rules: pass/fail and outcome classification (unit-tested, no I/O)."""
from typing import Any, Mapping, Optional, Tuple

# Outcome values (stored in the DB `outcome` column and used for alert routing)
DISPENSED = "dispensed"
MOTOR_NO_KIBBLE = "motor_no_kibble"  # motor ran, nothing came out: clog or empty hopper
NO_MOTOR = "no_motor"  # feeder never ran during the window
UNVERIFIED = "unverified"
CAMERA_UNAVAILABLE = "camera_unavailable"
BOWL_CHECK = "bowl_check"  # on-demand "is there food?" check, no dispense expected
# The camera stream stopped delivering audio before the feed could be heard: a capture failure,
# NOT evidence of a clog (never escalated, never auto-retried, never announced over TTS)
RECORDING_CUT_SHORT = "recording_cut_short"

CLOG_OUTCOMES = (MOTOR_NO_KIBBLE, NO_MOTOR, UNVERIFIED)
# Failed runs that are NOT a clog: nothing reliable was captured, so the feed is simply unverified
NOT_CLOG_OUTCOMES = (BOWL_CHECK, CAMERA_UNAVAILABLE, RECORDING_CUT_SHORT)
# Outcomes the consecutive-missed-feeds counter skips (they neither count nor reset the streak)
UNVERIFIED_OUTCOMES = (CAMERA_UNAVAILABLE, UNVERIFIED, RECORDING_CUT_SHORT)

DEFAULT_MIN_COVERAGE = 0.75
DEFAULT_MAX_TAIL_GAP_SEC = 5.0


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def recording_cut_short(
    session: Optional[Mapping[str, Any]],
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_tail_gap_sec: float = DEFAULT_MAX_TAIL_GAP_SEC,
) -> bool:
    """True if the camera stream stopped delivering audio well before the recording window ended.

    Uses the capture session's `recorded_wall_sec`, `audio_sec` and `audio_last_rx_sec`. Missing / unknown
    values (older capture paths, external streams) or very short windows (< 3 s) mean "can't tell" -> False.
    """
    if not session:
        return False
    wall = _num(session.get("recorded_wall_sec"))
    audio_sec = _num(session.get("audio_sec"))
    if wall is None or audio_sec is None or wall < 3.0:
        return False
    if audio_sec < min_coverage * wall:
        return True
    last_rx = _num(session.get("audio_last_rx_sec"))
    return last_rx is not None and (wall - last_rx) > max_tail_gap_sec


def decide_passed(
    audio_res,
    visual_res,
    feed_abnormal: int,
    expect_dispense: bool,
    vision_can_override_audio: bool = False,
) -> bool:
    """Pure pass/fail decision (audio first).

    The kibble "ting" (audio) is the primary evidence: it is there whether the bowl is empty or full, and the
    owner's experience is that it is reliable whenever the recording is complete.
    - Kibble heard -> pass (even if the recording was cut short; the amount just couldn't be fully verified).
    - Audio present, no kibble heard -> fail. Vision (IR / dog detectors give many false positives) may NOT flip
      it to a pass unless `vision_can_override_audio` (legacy behaviour).
    - No audio at all -> vision alone (on-demand checks also accept "food seen in bowl").
    Vision can never veto a feed the audio confirmed, because bowl appearance changes with lights / IR / the dog.
    """
    if feed_abnormal > 1:
        return False
    sound_passed = bool(audio_res and audio_res.kibble_dispensed_audio)
    vision_confirms = bool(visual_res is not None and (visual_res.food_increased or visual_res.food_present_llm is True))
    if audio_res is not None:
        if sound_passed:
            return True
        return bool(vision_can_override_audio and vision_confirms)
    # No audio captured: vision alone (on-demand checks also accept "food seen in bowl")
    if visual_res is None:
        return False
    return vision_confirms or (not expect_dispense and visual_res.food_present_cv)


def audio_state(audio_res) -> str:
    """Legacy `audio_state` DB value."""
    if not audio_res:
        return "unverified"
    if audio_res.kibble_dispensed_audio:
        return "dispensed"
    return "motor_only_clog" if audio_res.motor_detected else "silent"


def classify(audio_res, visual_res, expect_dispense: bool, passed: bool, cut_short: bool = False) -> Tuple[str, bool]:
    """Returns (outcome, passed). What went wrong matters to the owner: a feeder that never ran needs
    a different fix than a motor spinning on a jammed chute / empty hopper — and a camera stream that
    dropped mid-recording needs no fix at the feeder at all."""
    outcome = {"dispensed": DISPENSED, "motor_only_clog": MOTOR_NO_KIBBLE, "silent": NO_MOTOR}.get(
        audio_state(audio_res), UNVERIFIED
    )
    if audio_res is None and visual_res is None:
        # Nothing was captured: a camera/cloud problem, not evidence of a clog
        return CAMERA_UNAVAILABLE, passed
    if not expect_dispense:
        # On-demand check: silence is normal; the question is only "is there food?"
        return BOWL_CHECK, not (visual_res is not None and visual_res.bowl_empty)
    if audio_res is not None and cut_short and not passed:
        # The stream stopped before the feed could be heard: "no motor / no kibble" proves nothing
        return RECORDING_CUT_SHORT, False
    return outcome, passed


def status_label(outcome: str, passed: bool) -> str:
    if passed:
        return "ok"
    if outcome == RECORDING_CUT_SHORT:
        return "unverified"
    return "clog_detected" if outcome in CLOG_OUTCOMES else outcome


def is_clog(outcome: str, passed: bool) -> bool:
    return (not passed) and outcome not in NOT_CLOG_OUTCOMES


def is_unverified(outcome: str) -> bool:
    """Runs where nothing reliable was captured (no verdict about the feeder itself)."""
    return outcome in UNVERIFIED_OUTCOMES


def clog_alert_title(outcome: str) -> str:
    return {
        MOTOR_NO_KIBBLE: "FEEDER CLOG OR EMPTY HOPPER: motor ran but no kibble came out!",
        NO_MOTOR: "FEEDER DID NOT RUN: no motor sound during the feeding window!",
    }.get(outcome, "PUPPY FOOD NOT CONFIRMED IN BOWL!")
