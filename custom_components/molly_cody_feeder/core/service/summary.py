"""Flat Home Assistant summary payload builder (`summary.py`)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from custom_components.molly_cody_feeder.core.db import KibbleDatabase


def meal_result(ev: Dict[str, Any]) -> str:
    if ev.get("user_feedback") == "wrong":
        return "fed" if not ev.get("success") else "problem"
    if ev.get("success"):
        return "fed"
    if ev.get("outcome") in ("recording_cut_short", "camera_unavailable", "unverified"):
        return "unverified"
    return "problem"


def _flow_pct(ev: Optional[Dict[str, Any]]) -> Optional[int]:
    if not ev:
        return None
    try:
        cov = ((json.loads(ev.get("raw_json") or "{}").get("audio") or {}).get("flow_coverage"))
        if cov is not None and (ev.get("motor_duration_sec") or 0) > 0:
            return int(round(float(cov) * 100))
    except Exception:
        pass
    return None


def _short_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "—"
    try:
        return datetime.fromisoformat(str(iso_ts)).strftime("%-I:%M %p")
    except Exception:
        return str(iso_ts)[11:16] or "—"


def _minutes_since(unix_ts: Optional[int]) -> Optional[int]:
    if not unix_ts:
        return None
    return max(0, int((time.time() - int(unix_ts)) // 60))


def meal_timing_from_schedule(slots: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    now_dt = now or datetime.now()
    enabled = [s for s in (slots or []) if s.get("enabled")]
    next_dt: Optional[datetime] = None
    last_dt: Optional[datetime] = None
    next_portions = 0
    for s in enabled:
        h, m = int(s.get("hour", 0)), int(s.get("minute", 0))
        mask = int(s.get("days_mask", 0x7F))
        for day_offset in (-1, 0, 1, 2):
            cand = (now_dt + timedelta(days=day_offset)).replace(hour=h, minute=m, second=0, microsecond=0)
            if mask != 0 and not (mask & (1 << cand.weekday())):
                continue
            if cand <= now_dt and (last_dt is None or cand > last_dt):
                last_dt = cand
            if cand > now_dt and (next_dt is None or cand < next_dt):
                next_dt = cand
                next_portions = int(s.get("portions", 1))
    return {
        "next_meal_time": next_dt.strftime("%H:%M") if next_dt else None,
        "next_meal_at": next_dt.isoformat(timespec="seconds") if next_dt else None,
        "next_meal_portions": next_portions,
        "minutes_to_next_meal": int((next_dt - now_dt).total_seconds() // 60) if next_dt else None,
        "minutes_since_scheduled_meal": int((now_dt - last_dt).total_seconds() // 60) if last_dt else None,
    }


def compute_ha_summary(
    db: KibbleDatabase,
    schedule_slots: List[Dict[str, Any]],
    last_state: Dict[str, Any],
    hopper_capacity: int = 240,
    hopper_low_percent: float = 20.0,
    busy: bool = False,
    last_alert: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = int(time.time())
    today = datetime.now().strftime("%Y-%m-%d")
    events = db.get_recent_events(limit=200)
    meals = [e for e in events if e.get("event_type") != "manual_check"]
    last = meals[0] if meals else None

    today_meals = [e for e in meals if str(e.get("timestamp", "")).startswith(today)]
    today_Verdicts = [meal_result(e) for e in today_meals]
    week_cut = now - 7 * 86400
    week_meals = [e for e in meals if int(e.get("unix_time") or 0) >= week_cut]
    week_fed = sum(1 for e in week_meals if meal_result(e) == "fed")

    hop = db.hopper_status(hopper_capacity)
    week_days = max(1.0, min(7.0, (now - min((int(e["unix_time"]) for e in week_meals), default=now)) / 86400.0))
    daily_p = sum(int(e.get("portions") or 0) for e in week_meals if meal_result(e) == "fed") / week_days
    days_left = round(hop["portions_left"] / daily_p, 1) if hop["known"] and daily_p > 0.5 else None

    res = meal_result(last) if last else "none"
    last_txt = (
        f"Fed ({last['portions']}p) at {_short_time(last['timestamp'])}" if res == "fed" and last
        else (f"Couldn't verify at {_short_time(last['timestamp'])}" if res == "unverified" and last
              else (f"PROBLEM ({last.get('outcome')}) at {_short_time(last['timestamp'])}" if last else "No meals recorded yet"))
    )
    today_items = [
        f"{_short_time(e['timestamp'])}: {'Fed' if meal_result(e)=='fed' else ('Unverified' if meal_result(e)=='unverified' else 'PROBLEM')} ({e.get('portions') or 0}p)"
        for e in reversed(today_meals)
    ]
    flows_7d = [f for f in (_flow_pct(e) for e in week_meals) if f is not None]
    timing = meal_timing_from_schedule(schedule_slots)

    visual = last_state.get("visual") or {}
    anomaly = last_state.get("anomaly") or {}

    return {
        "status": last_state.get("status", "ok" if res in ("fed", "none") else res),
        "busy": bool(busy),
        "clog_detected": bool(last_state.get("clog_detected", False)),
        "last_meal_time": last.get("timestamp") if last else None,
        "last_meal_short_time": _short_time(last.get("timestamp")) if last else "—",
        "last_meal_result": res,
        "last_meal_outcome": last.get("outcome") if last else "none",
        "last_meal_portions": int(last.get("portions") or 0) if last else 0,
        "last_meal_text": last_txt,
        "last_meal_ok": res == "fed",
        "minutes_since_last_meal": _minutes_since(last.get("unix_time")) if last else None,
        "next_meal_time": timing["next_meal_time"],
        "next_meal_portions": timing["next_meal_portions"],
        "minutes_to_next_meal": timing["minutes_to_next_meal"],
        "today_meals": len(today_meals),
        "today_fed": today_Verdicts.count("fed"),
        "today_unverified": today_Verdicts.count("unverified"),
        "today_problems": today_Verdicts.count("problem"),
        "today_portions": sum(int(e.get("portions") or 0) for e in today_meals if meal_result(e) == "fed"),
        "today_list": " · ".join(today_items) if today_items else "No meals yet today",
        "week_meals": len(week_meals),
        "week_fed": week_fed,
        "week_confirmed_pct": int(round(week_fed * 100.0 / len(week_meals))) if week_meals else 100,
        "kibble_flow": _flow_pct(last),
        "kibble_flow_7d_avg": int(round(sum(flows_7d) / len(flows_7d))) if flows_7d else None,
        "last_motor_duration_sec": round(float(last.get("motor_duration_sec") or 0.0), 2) if last else 0.0,
        "bowl_fill_pct": round(float(last.get("after_fill_ratio") or 0.0) * 100.0, 1) if last else 0.0,
        "food_present": bool(visual.get("food_present_cv") or visual.get("food_increased") or (last and meal_result(last) == "fed")),
        "dog_at_bowl": bool(visual.get("dog_at_bowl_detected") or (last and last.get("dog_at_bowl"))),
        "is_infrared_mode": bool(visual.get("is_infrared_mode") or (last and last.get("is_infrared"))),
        "weak_flow_warning": bool(anomaly.get("weak_flow") or anomaly.get("low_flow") or anomaly.get("short_run")),
        "consecutive_missed_feeds": db.consecutive_missed_feeds(),
        "hopper_known": hop["known"],
        "hopper_capacity_portions": hop["capacity_portions"],
        "hopper_portions_used": hop["portions_used"],
        "hopper_portions_left": hop["portions_left"],
        "hopper_percent": hop["percent_left"],
        "hopper_days_left": days_left,
        "hopper_low": bool(hop["known"] and hop["percent_left"] <= hopper_low_percent),
        "last_alert_title": (last_alert or {}).get("title"),
        "last_alert_time": (last_alert or {}).get("timestamp"),
        "last_alert_event": (last_alert or {}).get("event"),
        "last_alert_urgent": bool((last_alert or {}).get("urgent", False)),
    }
