"""Domain policies: the robot-behavior rules, as pure predicates over DeviceStatus, and
the Overdue scoring rules over Room Intervals.

These are the interpretation of status; adapters only translate firmware codes into the
semantic fields these functions read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import Enum

from roborock_auto.domain.models import (
    CleaningMode,
    CleanSettings,
    DeviceStatus,
    FanPower,
    MopMode,
    Occurrence,
    OccurrenceState,
    RoomInterval,
    SegmentOverdue,
    WaterMode,
)

# States that mean "no information", not a robot state: a wifi/cloud blip reports unknown or
# device_offline with every flag cleared, which would otherwise read as cancelled off-dock.
INDETERMINATE_STATES = frozenset({"unknown", "device_offline", "locked"})


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
    Either way the robot is not progressing and will not come home on its own. An
    indeterminate state is neither: it carries no information, so it must not count as a
    stall — a long outage is bounded by the absolute return timeout instead."""
    if status.state_name in INDETERMINATE_STATES:
        return False
    if status.state_name == "paused":
        return True
    return not status.in_cleaning and not status.docked


def recharging_pending(status: DeviceStatus) -> bool:
    """The job was accepted but the robot is charging on the dock before leaving (deep
    discharge). The job is still pending — this is progress, not a dropped trigger."""
    return status.docked and status.in_cleaning


def _known[E: Enum](enum: type[E], value: str | None) -> E | None:
    try:
        return enum(value)
    except ValueError:
        return None


def setup_settings(status: DeviceStatus) -> CleanSettings:
    """What Setup currently has the robot on. A value this app has no choice for (a
    custom or smart mode, water off, suction off) reads as None and is not put back."""
    return CleanSettings(
        fan_power=_known(FanPower, status.fan_power),
        water_mode=_known(WaterMode, status.water_mode),
        mop_route=_known(MopMode, status.mop_route),
    )


def resolve_setup(stored: CleanSettings, robot: CleanSettings) -> CleanSettings:
    """Setup is the user's stored choice; a setting they never chose falls back to what the
    robot reports before the mode switch. The robot alone cannot be trusted with it: a
    mop-only clean leaves suction off and a vacuum-only clean leaves water off."""
    return CleanSettings(
        fan_power=stored.fan_power or robot.fan_power,
        water_mode=stored.water_mode or robot.water_mode,
        mop_route=stored.mop_route or robot.mop_route,
    )


def settings_after_mode_switch(
    mode: CleaningMode, chosen: CleanSettings, setup: CleanSettings
) -> CleanSettings:
    """A cleaning-mode switch overwrites fan, water and route with the mode's own defaults
    (Balanced, Standard water or none, Standard route), so the settings the clean should
    run with must be sent again after it: each chosen value, else Setup's. Fan power only
    when the mode vacuums, since sending it to a mop-only clean would turn suction back on;
    water and route only when the mode mops, leaving a vacuum-only clean dry."""
    return CleanSettings(
        fan_power=(chosen.fan_power or setup.fan_power) if mode.vacuums else None,
        water_mode=(chosen.water_mode or setup.water_mode) if mode.mops else None,
        mop_route=(chosen.mop_route or setup.mop_route) if mode.mops else None,
    )


# A busy or unready robot never consumes an attempt — that is not a failure — so these
# bound only genuine refusals: an exception that survived the adapter's transport retry.
OCCURRENCE_MAX_ATTEMPTS = 3
OCCURRENCE_RETRY_SPACING = timedelta(minutes=5)


def occurrence_expired(occurrence: Occurrence, now: datetime) -> bool:
    """A PENDING Occurrence whose Deadline has passed is abandoned rather than fired at
    a surprising hour. Only PENDING expires: every other state already resolved."""
    return occurrence.state is OccurrenceState.PENDING and now >= occurrence.deadline


def claimable_occurrences(occurrences: Sequence[Occurrence], now: datetime) -> list[Occurrence]:
    """The selector: every PENDING Occurrence ripe to fire — at or past its instant,
    inside its Deadline, and past the minimum spacing since its last failed attempt.
    Ordered by instant then identity, so simultaneous firings run in a total order and
    none is dropped for being second."""
    ripe = [
        o
        for o in occurrences
        if o.state is OccurrenceState.PENDING
        and o.scheduled_for <= now < o.deadline
        and (o.last_attempt_at is None or now - o.last_attempt_at >= OCCURRENCE_RETRY_SPACING)
    ]
    return sorted(ripe, key=lambda o: (o.scheduled_for, o.id))


def clean_priority(
    interval: timedelta, last_cleaned_at: datetime | None, now: datetime
) -> float | None:
    """How overdue a Segment is: time since its last Cleaning Run over its own Room
    Interval, so rooms on different Intervals compare directly. A never-cleaned Segment is
    maximally overdue, which is no finite number."""
    if last_cleaned_at is None:
        return None
    return (now - last_cleaned_at) / interval


def overdue_report(
    intervals: Sequence[RoomInterval],
    last_cleaned: Mapping[int, datetime],
    now: datetime,
) -> list[SegmentOverdue]:
    """Every Segment with a Room Interval, scored and sorted worst-first: never-cleaned
    above everything, then descending Priority, then Segment number so the order is
    total and a reload cannot reshuffle equal rooms."""
    entries = [
        SegmentOverdue(
            segment_id=interval.segment_id,
            interval=interval.interval,
            last_cleaned_at=last_cleaned.get(interval.segment_id),
            priority=clean_priority(
                interval.interval, last_cleaned.get(interval.segment_id), now
            ),
        )
        for interval in intervals
    ]
    return sorted(
        entries,
        key=lambda e: (e.priority is not None, -(e.priority or 0.0), e.segment_id),
    )
