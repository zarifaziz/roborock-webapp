"""Firmware code tables and pure translation helpers for the Roborock cloud adapter.

Everything here maps vendor codes to the semantic fields of our domain models; the
interpretation of those fields lives in domain/policies.py.
"""

from __future__ import annotations

from typing import Any

from roborock.data.v1 import RoborockInCleaning, RoborockStateCode

from roborock_auto.domain.models import CleaningMode, FanPower, MopMode

# Docked = charger connected. Codes from roborock.data.v1.RoborockStateCode.
DOCKED_STATES = {
    RoborockStateCode.charging,
    RoborockStateCode.charging_complete,
    RoborockStateCode.charging_problem,
}
ACTIVE_STATES = {
    RoborockStateCode.cleaning,
    RoborockStateCode.spot_cleaning,
    RoborockStateCode.zoned_cleaning,
    RoborockStateCode.segment_cleaning,
    RoborockStateCode.returning_home,
    RoborockStateCode.going_to_target,
}

# Param shapes to confirm on live bring-up. Non-destructive settings; wrong values only
# misconfigure cleaning intensity, they cannot damage anything.
FAN_LEVELS = {FanPower.QUIET: 101, FanPower.BALANCED: 102, FanPower.TURBO: 103, FanPower.MAX: 104}
MOP_MODES = {MopMode.STANDARD: 300, MopMode.DEEP: 301}

# Our semantic mode -> the library's device-aware CleaningMode name.
CLEANING_MODE_NAMES = {
    CleaningMode.VACUUM: "vacuum",
    CleaningMode.MOP: "mop",
    CleaningMode.VAC_AND_MOP: "vac_and_mop",
}


def state_name(code: RoborockStateCode | None) -> str:
    if code is None:
        return "unknown"
    return code.name


def is_docked(code: RoborockStateCode | None) -> bool:
    return code in DOCKED_STATES


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
