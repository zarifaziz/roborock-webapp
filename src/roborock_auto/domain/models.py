"""Value objects: plain data types that cross the port boundary.

No python-roborock types and no firmware magic integers appear here. Everything above the
ports depends only on this module, so a library change stays quarantined in the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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


class CleaningMode(Enum):
    """High-level clean type. Lets the two-stage clean vacuum then mop the whole house
    without needing separate saved routines."""

    VACUUM = "vacuum"
    MOP = "mop"
    VAC_AND_MOP = "vac_and_mop"


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
