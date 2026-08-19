"""Domain policies: the robot-behavior rules, as pure predicates over DeviceStatus.

These are the interpretation of status; adapters only translate firmware codes into the
semantic fields these functions read.
"""

from __future__ import annotations

from roborock_auto.domain.models import DeviceStatus


def has_departed(status: DeviceStatus) -> bool:
    """True once the robot has actually left the dock to run the job. Reads the adapter's
    semantic `cleaning` flag, so dock-servicing that lingers from the previous stage
    (emptying the bin, washing the mop) does not count — that let the mop stage 'depart' on
    leftover servicing and complete without mopping. An unmapped firmware state is not
    cleaning, so departure fails safe rather than falsely confirming."""
    return status.cleaning


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
