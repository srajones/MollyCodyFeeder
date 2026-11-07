"""DP 207 15-char schedule decoder & encoder for the Molly & Cody Smart Feeder."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("molly_cody_feeder.schedule_decoder")

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class MealEntry:
    slot: int = 0
    hour: int = 8
    minute: int = 0
    portions: int = 1
    days_mask: int = 0x7F
    enabled: bool = True
    suffix: str = "000000"

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def days_str(self) -> str:
        if self.days_mask == 0x7F:
            return "Everyday"
        if self.days_mask == 0x00:
            return "Once"
        active = [DAY_NAMES[i] for i in range(7) if self.days_mask & (1 << i)]
        return ",".join(active) if active else "Everyday"

    def to_molly_slot(self) -> str:
        en_char = "1" if self.enabled else "0"
        sfx = (self.suffix or "000000")[:6].ljust(6, "0")
        return f"{self.days_mask & 0x7F:02x}{self.hour:02d}{self.minute:02d}{self.portions:02d}{en_char}{sfx}"

    def to_dict(self) -> Dict[str, object]:
        raw_val = self.to_molly_slot()
        return {
            "slot": self.slot,
            "time": self.time_str,
            "hour": self.hour,
            "minute": self.minute,
            "portions": self.portions,
            "days_mask": self.days_mask,
            "days": self.days_str,
            "enabled": self.enabled,
            "raw": raw_val,
            "raw_slot": raw_val,
        }


def decode_molly_schedule(raw_str: str) -> List[MealEntry]:
    if not raw_str or not isinstance(raw_str, str):
        return []
    s = raw_str.strip()
    if len(s) < 15 or len(s) % 15 != 0:
        return []
    entries: List[MealEntry] = []
    for idx in range(0, len(s), 15):
        chunk = s[idx : idx + 15]
        slot_num = (idx // 15) + 1
        try:
            days_mask = int(chunk[0:2], 16)
            hour = int(chunk[2:4], 10)
            minute = int(chunk[4:6], 10)
            portions = int(chunk[6:8], 10)
            enabled = chunk[8] == "1"
            suffix = chunk[9:15]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return []
            entries.append(
                MealEntry(
                    slot=slot_num,
                    hour=hour,
                    minute=minute,
                    portions=portions,
                    days_mask=days_mask,
                    enabled=enabled,
                    suffix=suffix,
                )
            )
        except ValueError:
            return []
    return entries


def encode_molly_schedule(entries: List[MealEntry]) -> str:
    return "".join(e.to_molly_slot() for e in entries)


def update_molly_schedule_slot(
    current_raw: str,
    slot: int,
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    portions: Optional[int] = None,
    days_mask: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> str:
    if slot < 1 or slot > 8:
        raise ValueError("Slot must be between 1 and 8")
    entries = decode_molly_schedule(current_raw)
    if not entries and not current_raw:
        entries = [
            MealEntry(
                slot=i + 1,
                hour=8,
                minute=0,
                portions=1,
                days_mask=0,
                enabled=False,
            )
            for i in range(max(5, slot))
        ]
    while len(entries) < slot:
        entries.append(
            MealEntry(
                slot=len(entries) + 1,
                hour=8,
                minute=0,
                portions=1,
                days_mask=0x7F,
                enabled=False,
            )
        )
    target = entries[slot - 1]
    if hour is not None:
        target.hour = max(0, min(23, int(hour)))
    if minute is not None:
        target.minute = max(0, min(59, int(minute)))
    if portions is not None:
        target.portions = max(1, min(20, int(portions)))
    if days_mask is not None:
        target.days_mask = int(days_mask) & 0x7F
    if enabled is not None:
        target.enabled = bool(enabled)
    return encode_molly_schedule(entries)


def parse_days_spec(spec: str) -> int:
    s = spec.strip().lower()
    if s in ("everyday", "all", "daily", "7f", "0x7f"):
        return 0x7F
    if s in ("once", "none", "0", "00", "0x00"):
        return 0x00
    if s in ("weekdays", "mon-fri"):
        return 0x1F
    if s in ("weekends", "sat-sun"):
        return 0x60
    try:
        if s.startswith("0x"):
            return int(s, 16) & 0x7F
    except ValueError:
        pass
    mask = 0
    day_lookup = {name.lower(): idx for idx, name in enumerate(DAY_NAMES)}
    for part in s.split(","):
        p = part.strip()[:3].lower()
        if p in day_lookup:
            mask |= 1 << day_lookup[p]
    return mask if mask else 0x7F


def decode_meal_plan(raw_value: str) -> List[MealEntry]:
    if not raw_value or not isinstance(raw_value, str):
        return []
    molly_entries = decode_molly_schedule(raw_value)
    if molly_entries:
        return molly_entries
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, list):
            entries = []
            for idx, item in enumerate(parsed, start=1):
                t_str = str(item.get("time", "00:00"))
                hh, mm = (int(x) for x in t_str.split(":"))
                entries.append(
                    MealEntry(
                        slot=int(item.get("slot", idx)),
                        hour=hh,
                        minute=mm,
                        portions=int(item.get("portions", 1)),
                        days_mask=int(item.get("loops", 0x7F)),
                        enabled=bool(item.get("enabled", True)),
                    )
                )
            return entries
    except Exception:
        pass
    try:
        raw_bytes = base64.b64decode(raw_value)
        entries = []
        if len(raw_bytes) >= 5 and len(raw_bytes) % 5 == 0:
            for idx, i in enumerate(range(0, len(raw_bytes), 5), start=1):
                days_mask, hour, minute, portions, enabled = raw_bytes[i : i + 5]
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    entries.append(
                        MealEntry(
                            slot=idx,
                            hour=hour,
                            minute=minute,
                            portions=portions,
                            days_mask=days_mask,
                            enabled=bool(enabled),
                        )
                    )
            return entries
    except Exception as exc:
        logger.debug("Could not decode meal_plan value '%s': %s", raw_value, exc)
    return []
