"""Value objects: plain data types that cross the port boundary.

No python-roborock types and no firmware magic integers appear here. Everything above the
ports depends only on this module, so a library change stays quarantined in the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import Enum
from typing import Protocol, runtime_checkable


class CarpetMode(Enum):
    RISE = "rise"          # mop lifts, vacuum still cleans the carpet
    AVOID = "avoid"        # robot skips carpet entirely
    UNKNOWN = "unknown"    # device reported a value we do not have a name for


class FanPower(Enum):
    QUIET = "quiet"
    BALANCED = "balanced"
    TURBO = "turbo"
    MAX = "max"


class MopMode(Enum):
    STANDARD = "standard"
    DEEP = "deep"


class WaterMode(Enum):
    """Mop water-flow intensity: how wet the mop runs."""

    MILD = "mild"
    STANDARD = "standard"
    INTENSE = "intense"


class CleaningMode(Enum):
    """High-level clean type. Lets the two-stage clean vacuum then mop the whole house
    without needing separate saved routines."""

    VACUUM = "vacuum"
    MOP = "mop"
    VAC_AND_MOP = "vac_and_mop"


class CleanSource(Enum):
    """Which entry point started a Clean, so recorded runs can tell the user's own
    actions apart from automated ones."""

    APP = "app"
    CRON = "cron"


class ConsumablePart(Enum):
    """A user-serviceable consumable whose life counter can be reset after the owner
    replaces (brushes, filter) or cleans (sensors) it."""

    MAIN_BRUSH = "main_brush"
    SIDE_BRUSH = "side_brush"
    FILTER = "filter"
    SENSOR = "sensor"


@dataclass(frozen=True)
class DeviceStatus:
    state_name: str            # normalized: "cleaning", "charging", "idle", "error", ...
    battery: int
    in_cleaning: bool          # true while a job is active, even if temporarily docked to charge
    docked: bool
    error: str | None = None
    # Settings readback + clean telemetry + dock health, straight from the status poll.
    # All optional: a firmware/library that lacks a field reports None, never crashes.
    fan_power: str | None = None      # friendly current fan speed, e.g. "balanced"
    mop_route: str | None = None      # friendly current mop route, e.g. "standard"
    water_mode: str | None = None     # friendly current mop water flow, e.g. "standard"
    clean_area_m2: float | None = None
    clean_time_s: int | None = None
    dock_error: str | None = None     # human-ish dock error, e.g. "clean water tank empty", else None
    # True only while the robot is physically out running the clean (heading to a room,
    # vacuuming, mopping) — NOT while docked-recharging or servicing the dock (emptying the
    # bin, washing the mop). Classified in the adapter from the firmware state; an unknown
    # state is False so departure fails safe. Distinct from in_cleaning, which stays true
    # through recharge and servicing.
    cleaning: bool = False


@dataclass(frozen=True)
class CleaningRun:
    """The durable trace of a Clean: this app started one, from `source`, over
    `segment_ids` (None = whole house), in `mode`, at `started_at` (timezone-aware).
    It records a start, not a completion (docs/adr/0003)."""

    source: CleanSource
    mode: CleaningMode
    segment_ids: tuple[int, ...] | None
    started_at: datetime


@dataclass(frozen=True)
class RoomInterval:
    """How often one Segment should be cleaned: the user's standing intent about a room.
    A duration rather than a day count, so twice-daily is as expressible as weekly."""

    segment_id: int
    interval: timedelta

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("Room Interval must be a positive duration")


@dataclass(frozen=True)
class SegmentOverdue:
    """One Segment's line in the Overdue report: its Interval, when it was last cleaned
    (derived from Cleaning Runs, docs/adr/0003), and its Priority."""

    segment_id: int
    interval: timedelta
    last_cleaned_at: datetime | None
    priority: float | None

    @property
    def never_cleaned(self) -> bool:
        return self.last_cleaned_at is None

    @property
    def overdue(self) -> bool:
        """Never cleaned is maximally overdue; otherwise a full Interval has elapsed."""
        return self.priority is None or self.priority >= 1.0


class Weekday(Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @classmethod
    def of(cls, day: date) -> Weekday:
        return list(cls)[day.weekday()]


@runtime_checkable
class RecurrenceSchedule(Protocol):
    """The "when" half of a Schedule Rule: a rule for generating instants."""

    def occurrences_between(self, start: datetime, end: datetime, tz: tzinfo) -> list[datetime]:
        """The UTC instants this Recurrence implies within [start, end), in order."""
        ...


@dataclass(frozen=True)
class WeeklyGrid:
    """A Recurrence as a weekly grid: a set of Weekdays plus one wall-clock time of day.
    A grid with no days can never fire, which is a typo, not a paused Rule — the enabled
    flag already expresses "paused"."""

    days: frozenset[Weekday]
    at: time

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("a Schedule Rule needs at least one day; disable it to pause it")
        if self.at.tzinfo is not None:
            # Generation attaches the household zone itself; an offset here would be
            # silently overridden, firing the Rule hours away from what it claims.
            raise ValueError("recurrence time of day is wall-clock; it cannot carry a timezone")

    def occurrences_between(self, start: datetime, end: datetime, tz: tzinfo) -> list[datetime]:
        # The conversion order is fixed: convert the window to local, walk local calendar
        # days, combine each date with the wall-clock time, attach the zone, convert back
        # to UTC, and compare only in UTC. Local wall time never leaves this method.
        day = start.astimezone(tz).date()
        last_day = end.astimezone(tz).date()
        instants = []
        while day <= last_day:
            if Weekday.of(day) in self.days:
                instant = datetime.combine(day, self.at, tzinfo=tz).astimezone(UTC)
                if start <= instant < end:
                    instants.append(instant)
            day += timedelta(days=1)
        return instants


@dataclass(frozen=True)
class CleanAction:
    """The "what" half of a Schedule Rule, and the only Action understood today: clean
    `segment_ids` (None = whole house) in `mode`."""

    mode: CleaningMode
    segment_ids: tuple[int, ...] | None

    def __post_init__(self) -> None:
        if self.segment_ids is not None and not self.segment_ids:
            raise ValueError("an empty Segment list is ambiguous; use null for the whole house")


# The discriminated union of Actions, with exactly one member today.
Action = CleanAction


@dataclass(frozen=True)
class ScheduleRule:
    """A user's standing intent: a Recurrence paired with an Action. It describes what
    should happen repeatedly; it never records what did."""

    id: int
    name: str
    recurrence: RecurrenceSchedule
    action: Action
    enabled: bool


class OccurrenceState(Enum):
    """The lifecycle of an Occurrence. "Skipped" is deliberately not a state: a blocked
    firing stays PENDING with a reason attached, because the intent is still live. The
    claimed state is CLAIMED, not RUNNING — it is the scheduler's lock on the record, and
    "running" already means the robot cleaning."""

    PENDING = "pending"
    CLAIMED = "claimed"
    FIRED = "fired"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Occurrence:
    """One specific firing of one Schedule Rule at one instant, as a durable record with
    its own lifecycle. Carries its own Deadline — how stale the intent may become before
    it is abandoned — so a per-Rule override is later additive. The Rule's current name
    rides along for the timeline; the store fills it on read."""

    id: int
    rule_id: int
    rule_name: str
    scheduled_for: datetime
    deadline: datetime
    state: OccurrenceState
    attempts: int
    last_attempt_at: datetime | None
    blocked_reason: str | None
    fired_at: datetime | None


@dataclass(frozen=True)
class Room:
    segment_id: int
    name: str
    # Normalized (x0, y0, x1, y1) fractions 0..1 of the rendered map image, top-left origin,
    # for click-to-select overlays. None when the geometry is unknown (e.g. list_rooms).
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Routine:
    id: int
    name: str


@dataclass(frozen=True)
class Consumables:
    main_brush_pct: int
    side_brush_pct: int
    filter_pct: int
    sensor_pct: int | None = None
    # Wash-dock consumables; None on models without a dock (or older firmware).
    strainer_pct: int | None = None
    cleaning_brush_pct: int | None = None


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class MapData:
    rooms: list[Room] = field(default_factory=list)
    robot: Point | None = None
    # width/height of the rendered PNG, so the frontend can place overlays
    image_width: int = 0
    image_height: int = 0
