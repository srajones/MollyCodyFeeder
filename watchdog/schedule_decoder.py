import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("poor_puppy.schedule_decoder")

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


@dataclass
class MealEntry:
    days_mask: int          # 7-bit mask (0x7F = Everyday, 0x00 = Once)
    hour: int               # 0-23
    minute: int             # 0-59
    portions: int           # 1-20
    enabled: bool
    slot: int = 0           # 1-based slot index
    suffix: str = "000000"  # 6-char Molly & Cody slot trailer

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def days_str(self) -> str:
        mask = self.days_mask & 0x7F
        if mask == 0x7F:
            return "Everyday"
        if mask == 0x00:
            return "Once"
        # Bit 0..6 mapping
        active = []
        for idx, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
            if mask & (1 << idx):
                active.append(name)
        return ",".join(active)

    def to_molly_slot(self) -> str:
        """Encodes this MealEntry into a 15-character Molly & Cody DP 207 slot."""
        dd = f"{self.days_mask & 0x7F:02x}"
        hh = f"{max(0, min(23, self.hour)):02d}"
        mm = f"{max(0, min(59, self.minute)):02d}"
        pp = f"{max(1, min(99, self.portions)):02d}"
        en = "1" if self.enabled else "0"
        sfx = (self.suffix or "000000")[:6].ljust(6, "0")
        return f"{dd}{hh}{mm}{pp}{en}{sfx}"

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "time": self.time_str,
            "hour": self.hour,
            "minute": self.minute,
            "portions": self.portions,
            "enabled": self.enabled,
            "days_mask": f"0x{self.days_mask:02x}",
            "days": self.days_str,
        }


def parse_days_spec(spec: str) -> int:
    """
    Parses a human-friendly days string ('everyday', 'once', 'weekdays', 'weekends',
    'mon,wed,fri', or hex '0x7f') into a 7-bit days_mask.
    """
    s = spec.strip().lower()
    if s in ("everyday", "all", "daily", "7f", "0x7f"):
        return 0x7F
    if s in ("once", "none", "0", "00", "0x00"):
        return 0x00
    if s == "weekdays":
        return 0x1F
    if s == "weekends":
        return 0x60
    if s.startswith("0x"):
        return int(s, 16) & 0x7F

    mapping = {
        "mon": 0, "monday": 0,
        "tue": 1, "tues": 1, "tuesday": 1,
        "wed": 2, "wednesday": 2,
        "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
        "fri": 4, "friday": 4,
        "sat": 5, "saturday": 5,
        "sun": 6, "sunday": 6,
    }
    mask = 0
    for part in s.split(","):
        p = part.strip()
        if p in mapping:
            mask |= 1 << mapping[p]
    return mask if mask > 0 else 0x7F


def decode_molly_schedule(raw_str: str) -> List[MealEntry]:
    """
    Decodes the Molly & Cody (and.mollycody.app01) DP 207 schedule string.
    Format: N slots of 15 characters each:
      [0:2]  dd  = 2-char hex days bitmask (e.g. '7f' = everyday, '00' = once)
      [2:4]  HH  = 2-char decimal hour (00..23)
      [4:6]  MM  = 2-char decimal minute (00..59)
      [6:8]  PP  = 2-char decimal portions (01..20)
      [8:9]  E   = 1-char enabled flag ('1' = enabled, '0' = disabled)
      [9:15] sfx = 6-char reserved suffix ('000000')
    Example (75 chars = 5 slots):
      7f0700201000000 7f1100201000000 7f1600201000000 7f1900171000000 000908100000000
    """
    if not isinstance(raw_str, str):
        return []
    s = raw_str.strip()
    if len(s) < 15 or len(s) % 15 != 0:
        return []

    meals: List[MealEntry] = []
    for idx in range(0, len(s), 15):
        chunk = s[idx : idx + 15]
        try:
            days_mask = int(chunk[0:2], 16)
            hour = int(chunk[2:4], 10)
            minute = int(chunk[4:6], 10)
            portions = int(chunk[6:8], 10)
            enabled = chunk[8] == "1"
            suffix = chunk[9:15]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return []
            meals.append(
                MealEntry(
                    days_mask=days_mask,
                    hour=hour,
                    minute=minute,
                    portions=portions,
                    enabled=enabled,
                    slot=(idx // 15) + 1,
                    suffix=suffix,
                )
            )
        except ValueError:
            return []
    return meals


def encode_molly_schedule(meals: List[MealEntry]) -> str:
    """Encodes a list of MealEntry objects into a Molly & Cody DP 207 string."""
    return "".join(m.to_molly_slot() for m in meals)


def update_molly_schedule_slot(
    current_raw: str,
    slot: int,
    hour: Optional[int] = None,
    minute: Optional[int] = None,
    portions: Optional[int] = None,
    days_mask: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> str:
    """
    Updates a 1-based slot (1..8) in a Molly & Cody DP 207 schedule string and returns
    the new DP 207 string.
    """
    if slot < 1 or slot > 8:
        raise ValueError("Slot must be between 1 and 8")

    meals = decode_molly_schedule(current_raw)
    if not meals:
        meals = [
            MealEntry(days_mask=0, hour=8, minute=0, portions=1, enabled=False, slot=i + 1)
            for i in range(max(5, slot))
        ]
    while len(meals) < slot:
        meals.append(
            MealEntry(
                days_mask=0x7F,
                hour=8,
                minute=0,
                portions=1,
                enabled=False,
                slot=len(meals) + 1,
            )
        )

    target = meals[slot - 1]
    if hour is not None:
        target.hour = int(hour)
    if minute is not None:
        target.minute = int(minute)
    if portions is not None:
        target.portions = int(portions)
    if days_mask is not None:
        target.days_mask = int(days_mask)
    if enabled is not None:
        target.enabled = bool(enabled)

    return encode_molly_schedule(meals)


def decode_meal_plan(raw_value: Any) -> List[MealEntry]:
    """
    Decodes a Molly & Cody (DP 207) or standard Tuya Smart Pet Feeder (`cwwsq`) schedule.
    """
    if not raw_value:
        return []

    if isinstance(raw_value, list):
        return _parse_json_meals(raw_value)

    if isinstance(raw_value, str):
        stripped = raw_value.strip()

        # 1. Try Molly & Cody 15-char-per-slot format first (DP 207)
        molly_meals = decode_molly_schedule(stripped)
        if molly_meals:
            return molly_meals

        # 2. Try JSON array/object
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                return _parse_json_meals(parsed if isinstance(parsed, list) else parsed.get("meals", []))
            except Exception:
                pass

        # 3. Try Base64 decoding (standard Tuya raw DP format)
        try:
            data = base64.b64decode(stripped)
            meals = _parse_binary_meals(data)
            if meals:
                return meals
        except Exception:
            pass

        # 4. Try Hex decoding
        try:
            data = bytes.fromhex(stripped)
            meals = _parse_binary_meals(data)
            if meals:
                return meals
        except Exception:
            pass

    return []


def _parse_binary_meals(data: bytes) -> List[MealEntry]:
    meals: List[MealEntry] = []
    if not data:
        return meals

    if len(data) % 5 == 0:
        valid = True
        for i in range(0, len(data), 5):
            days_mask, hour, minute, portions, state = data[i : i + 5]
            if hour > 23 or minute > 59:
                valid = False
                break
            meals.append(
                MealEntry(
                    days_mask=days_mask,
                    hour=hour,
                    minute=minute,
                    portions=portions,
                    enabled=bool(state > 0 and portions > 0),
                    slot=(i // 5) + 1,
                )
            )
        if valid:
            return meals
        meals.clear()

    if len(data) % 4 == 0:
        for i in range(0, len(data), 4):
            days_mask, hour, minute, portions = data[i : i + 4]
            if hour <= 23 and minute <= 59:
                meals.append(
                    MealEntry(
                        days_mask=days_mask & 0x7F,
                        hour=hour,
                        minute=minute,
                        portions=portions,
                        enabled=bool(portions > 0 and (days_mask & 0x7F) > 0),
                        slot=(i // 4) + 1,
                    )
                )
    return meals


def _parse_json_meals(items: List[Any]) -> List[MealEntry]:
    meals: List[MealEntry] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        time_val = str(item.get("time", "00:00"))
        if ":" in time_val:
            h_str, m_str = time_val.split(":", 1)
            hour, minute = int(h_str), int(m_str)
        else:
            hour = int(item.get("hour", 0))
            minute = int(item.get("minute", 0))
        portions = int(item.get("portions", item.get("amount", 1)))
        enabled = bool(item.get("enabled", item.get("status", True)))
        meals.append(
            MealEntry(
                days_mask=int(item.get("loops", item.get("days", 0x7F)) if isinstance(item.get("loops"), int) else 0x7F),
                hour=hour,
                minute=minute,
                portions=portions,
                enabled=enabled,
                slot=int(item.get("slot", idx + 1)),
            )
        )
    return meals
