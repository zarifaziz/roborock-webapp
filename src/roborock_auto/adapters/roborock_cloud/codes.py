"""Firmware code tables and pure translation helpers for the Roborock cloud adapter.

Everything here maps vendor codes to the semantic fields of our domain models; the
interpretation of those fields lives in domain/policies.py.
"""

from __future__ import annotations

from typing import Any

from roborock.data.v1 import RoborockInCleaning, RoborockStateCode
from roborock.devices.traits.v1.consumeable import ConsumableAttribute

from roborock_auto.domain.models import CleaningMode, ConsumablePart, FanPower, MopMode, WaterMode

# Docked = charger connected. Codes from roborock.data.v1.RoborockStateCode.
DOCKED_STATES = {
    RoborockStateCode.charging,
    RoborockStateCode.charging_complete,
    RoborockStateCode.charging_problem,
}
# States where the robot is physically out running the clean: heading to a room, vacuuming,
# or mopping. This is an allowlist on purpose — a state we do not recognize is NOT treated as
# cleaning, so departure detection fails safe (a loud timeout) instead of falsely "departing"
# on an unmapped firmware state. Built by name so a code missing on some firmware is skipped.
_CLEANING_STATE_NAMES = [
    "cleaning", "spot_cleaning", "zoned_cleaning", "segment_cleaning", "going_to_target",
    # Qrevo mopping states (6301-6310).
    "robot_status_mopping", "clean_mop_cleaning", "clean_mop_mopping",
    "segment_mopping", "segment_clean_mop_cleaning", "segment_clean_mop_mopping",
    "zoned_mopping", "zoned_clean_mop_cleaning", "zoned_clean_mop_mopping",
]
# Busy, but at or near the dock, not out cleaning: returning, docking, emptying the bin,
# washing/attaching the mop. A job is not finished while any of these hold.
_SERVICING_STATE_NAMES = [
    "returning_home", "docking",
    "emptying_the_bin", "washing_the_mop", "washing_the_mop_2",
    "going_to_wash_the_mop", "attaching_the_mop", "detaching_the_mop",
    "back_to_dock_washing_duster",
]
CLEANING_STATES = {
    getattr(RoborockStateCode, n) for n in _CLEANING_STATE_NAMES if hasattr(RoborockStateCode, n)
}
# ACTIVE = out cleaning OR servicing the dock; drives derive_in_cleaning's state fallback.
ACTIVE_STATES = CLEANING_STATES | {
    getattr(RoborockStateCode, n) for n in _SERVICING_STATE_NAMES if hasattr(RoborockStateCode, n)
}

# Param shapes to confirm on live bring-up. Non-destructive settings; wrong values only
# misconfigure cleaning intensity, they cannot damage anything.
FAN_LEVELS = {FanPower.QUIET: 101, FanPower.BALANCED: 102, FanPower.TURBO: 103, FanPower.MAX: 104}
MOP_MODES = {MopMode.STANDARD: 300, MopMode.DEEP: 301}
WATER_MODES = {WaterMode.MILD: 201, WaterMode.STANDARD: 202, WaterMode.INTENSE: 203}

# Our semantic mode -> the library's device-aware CleaningMode name.
CLEANING_MODE_NAMES = {
    CleaningMode.VACUUM: "vacuum",
    CleaningMode.MOP: "mop",
    CleaningMode.VAC_AND_MOP: "vac_and_mop",
}

# Our semantic consumable part -> the library's ConsumableAttribute for RESET_CONSUMABLE.
CONSUMABLE_ATTRS = {
    ConsumablePart.MAIN_BRUSH: ConsumableAttribute.MAIN_BRUSH_WORK_TIME,
    ConsumablePart.SIDE_BRUSH: ConsumableAttribute.SIDE_BRUSH_WORK_TIME,
    ConsumablePart.FILTER: ConsumableAttribute.FILTER_WORK_TIME,
    ConsumablePart.SENSOR: ConsumableAttribute.SENSOR_DIRTY_TIME,
}


def state_name(code: RoborockStateCode | None) -> str:
    if code is None:
        return "unknown"
    return code.name


def is_docked(code: RoborockStateCode | None) -> bool:
    return code in DOCKED_STATES


def is_cleaning(code: RoborockStateCode | None) -> bool:
    """The robot is physically out running the clean (not docked, not servicing)."""
    return code in CLEANING_STATES


def derive_in_cleaning(code: RoborockStateCode | None, flag: RoborockInCleaning | None) -> bool:
    """A job is active if the in_cleaning flag says so (this survives a mid-job recharge,
    where the state flips to charging but the flag stays non-complete), or the state itself
    is an active-cleaning state.
    """
    if flag is not None and flag != RoborockInCleaning.complete:
        return True
    return code in ACTIVE_STATES


def error_str(error_code: Any) -> str | None:
    if error_code is None:
        return None
    name = getattr(error_code, "name", str(error_code))
    return None if name in ("none", "ok", "0") else name


# Wash-dock errors worth a friendlier phrase than the enum name. Anything unmapped falls
# back to the enum name with underscores replaced, so a new code is still shown, just rawer.
_DOCK_ERROR_MESSAGES = {
    "water_empty": "clean water tank empty",           # RoborockDockErrorCode 38
    "waste_water_tank_full": "waste water tank full",  # RoborockDockErrorCode 39
}


def dock_error_str(code: Any) -> str | None:
    """RoborockDockErrorCode -> human-ish message, or None when there is no error."""
    if code is None:
        return None
    name = getattr(code, "name", str(code))
    if name in ("ok", "none", "0"):
        return None
    return _DOCK_ERROR_MESSAGES.get(name, name.replace("_", " "))


def extract_carpet_value(resp: Any) -> Any:
    """Pull the int out of [{"carpet_clean_mode": n}] (or a bare int, defensively)."""
    if isinstance(resp, list) and resp:
        resp = resp[0]
    if isinstance(resp, dict):
        return resp.get("carpet_clean_mode")
    return resp


def life_pct(work_time_seconds: Any, max_hours: float) -> int:
    """Percent of life remaining from seconds of use against a standard lifetime."""
    used_hours = (work_time_seconds or 0) / 3600
    return max(0, min(100, round(100 - used_hours / max_hours * 100)))
