"""Domain policies: the robot-behavior rules, as pure predicates over DeviceStatus.

These are the interpretation of status; adapters only translate firmware codes into the
semantic fields these functions read.
"""

from __future__ import annotations

from roborock_auto.domain.models import DeviceStatus


def job_is_active(status: DeviceStatus) -> bool:
    """A mid-job recharge keeps the job active: docked+charging does not mean done."""
    return status.in_cleaning


def home_and_idle(status: DeviceStatus) -> bool:
    return status.docked and not status.in_cleaning


def ready_to_start(status: DeviceStatus) -> bool:
    return status.docked and not status.in_cleaning and status.error is None


def job_stalled(status: DeviceStatus) -> bool:
    """Paused mid-job, or the job ended somewhere other than the dock (user cancelled).
    Either way the robot is not progressing and will not come home on its own."""
    if status.state_name == "paused":
        return True
    return not status.in_cleaning and not status.docked
