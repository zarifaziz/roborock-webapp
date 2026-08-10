"""In-memory gateway: canned data and a clock-driven state machine.

Serves two jobs from one class: the local-run backend when no account is configured, and the
test double. Progress is a function of elapsed clock time, never of how many times you poll,
so a curl and the app's poller see the same thing. Tests inject a clock and, for edge cases,
a scripted timeline.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from roborock_auto.gateway.sample_map import SAMPLE_MAP_PNG
from roborock_auto.models import (
    CarpetMode,
    Consumables,
    DeviceStatus,
    FanPower,
    MapData,
    MopMode,
    Point,
    Room,
    Routine,
)

_ROOMS = [
    Room(16, "Living Room"),
    Room(17, "Kitchen"),
    Room(18, "Bedroom"),
    Room(19, "Hallway"),
]
_ROUTINES = [
    Routine(1001, "Vacuum whole house"),
    Routine(1002, "Mop whole house"),
]

DEPART_DELAY = 3.0     # seconds the robot lingers docked after start before it leaves
CLEAN_DURATION = 30.0  # seconds a job runs in machine mode


class FakeGateway:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._job_start: float | None = None
        self._carpet = CarpetMode.AVOID
        self._fan = FanPower.BALANCED
        self._mop = MopMode.STANDARD
        self._battery = 100
        # Scripted mode for tests: (elapsed_since_t0, DeviceStatus), evaluated against _t0.
        self._timeline: list[tuple[float, DeviceStatus]] | None = None
        self._t0 = clock()
        # Recorded commands so tests can assert what the automation sent.
        self.commands: list[tuple] = []

    # --- test hooks -------------------------------------------------------
    def set_timeline(self, entries: list[tuple[float, DeviceStatus]]) -> None:
        """Drive get_status from a scripted, clock-relative sequence."""
        self._timeline = sorted(entries, key=lambda e: e[0])
        self._t0 = self._clock()

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        self.commands.append(("connect",))

    async def close(self) -> None:
        self.commands.append(("close",))

    # --- reads ------------------------------------------------------------
    async def get_status(self) -> DeviceStatus:
        if self._timeline is not None:
            elapsed = self._clock() - self._t0
            current = self._timeline[0][1]
            for threshold, status in self._timeline:
                if elapsed >= threshold:
                    current = status
                else:
                    break
            return current
        return self._machine_status()

    def _machine_status(self) -> DeviceStatus:
        if self._job_start is None:
            return DeviceStatus("charging", self._battery, in_cleaning=False, docked=True)
        elapsed = self._clock() - self._job_start
        if elapsed >= CLEAN_DURATION:
            self._job_start = None
            return DeviceStatus("charging", self._battery, in_cleaning=False, docked=True)
        if elapsed < DEPART_DELAY:
            # Job accepted but robot still on the dock: the "looks docked right after
            # trigger" case. in_cleaning is already true, which is what keeps the
            # completion predicate from firing early.
            return DeviceStatus("charging", self._battery, in_cleaning=True, docked=True)
        return DeviceStatus("cleaning", self._battery, in_cleaning=True, docked=False)

    async def get_map_png(self) -> bytes:
        return SAMPLE_MAP_PNG

    async def get_map_data(self) -> MapData:
        return MapData(rooms=_ROOMS, robot=Point(50, 50), image_width=100, image_height=100)

    async def list_rooms(self) -> list[Room]:
        return list(_ROOMS)

    async def list_routines(self) -> list[Routine]:
        return list(_ROUTINES)

    async def get_consumables(self) -> Consumables:
        return Consumables(main_brush_pct=82, side_brush_pct=64, filter_pct=71)

    async def get_carpet_mode(self) -> CarpetMode:
        return self._carpet

    # --- commands ---------------------------------------------------------
    async def start(self) -> None:
        self.commands.append(("start",))
        self._job_start = self._clock()

    async def pause(self) -> None:
        self.commands.append(("pause",))
        self._job_start = None

    async def dock(self) -> None:
        self.commands.append(("dock",))
        self._job_start = None

    async def clean_rooms(self, segment_ids: list[int]) -> None:
        self.commands.append(("clean_rooms", tuple(segment_ids)))
        self._job_start = self._clock()

    async def set_fan_power(self, power: FanPower) -> None:
        self.commands.append(("set_fan_power", power))
        self._fan = power

    async def set_mop_mode(self, mode: MopMode) -> None:
        self.commands.append(("set_mop_mode", mode))
        self._mop = mode

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        self.commands.append(("set_carpet_mode", mode))
        self._carpet = mode

    async def run_routine(self, routine_id: int) -> None:
        self.commands.append(("run_routine", routine_id))
        self._job_start = self._clock()
