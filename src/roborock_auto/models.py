"""Plain data types that cross the Gateway boundary.

No python-roborock types and no firmware magic integers appear here. Both the app and the
automation depend only on this module, so a library change stays quarantined in LiveGateway.
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


@dataclass(frozen=True)
class DeviceStatus:
    state_name: str            # normalized: "cleaning", "charging", "idle", "error", ...
    battery: int
    in_cleaning: bool          # true while a job is active, even if temporarily docked to charge
    docked: bool
    error: str | None = None


@dataclass(frozen=True)
class Room:
    segment_id: int
    name: str


@dataclass(frozen=True)
class Routine:
    id: int
    name: str


@dataclass(frozen=True)
class Consumables:
    main_brush_pct: int
    side_brush_pct: int
    filter_pct: int


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
