"""In-memory driven adapter: canned data and a clock-driven state machine.

Serves two jobs from one class: the local-run backend when no account is configured, and the
test double. Progress is a function of elapsed clock time, never of how many times you poll,
so a curl and the app's poller see the same thing. Tests inject a clock and, for edge cases,
a scripted timeline.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from roborock_auto.adapters.fake import floor_plan, map_render
from roborock_auto.adapters.map_text import TextRoom, render_map_text
from roborock_auto.domain.cleaning_history import CleanRecord
from roborock_auto.domain.models import (
    AccountDevice,
    CarpetMode,
    CleaningMode,
    ConsumablePart,
    Consumables,
    DeviceStatus,
    FanPower,
    MapData,
    MopMode,
    Point,
    Room,
    Routine,
    WaterMode,
)

_ROOMS = [Room(r.segment_id, r.name) for r in floor_plan.ROOMS]


def _bbox(room: floor_plan.PlanRoom) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = room.bounds
    (px0, py0), (px1, py1) = map_render.to_image((x0, y0)), map_render.to_image((x1, y1))
    size = map_render.IMAGE_SIZE
    return round(px0 / size, 4), round(py0 / size, 4), round(px1 / size, 4), round(py1 / size, 4)


_MAP_ROOMS = [
    Room(r.segment_id, r.name, bbox=_bbox(r), color="#{:02x}{:02x}{:02x}".format(*r.color))
    for r in floor_plan.ROOMS
]
_ROUTINES = [
    Routine(1001, "Vacuum whole house"),
    Routine(1002, "Mop whole house"),
]

DEPART_DELAY = 3.0     # seconds the robot lingers docked after start before it leaves
CLEAN_DURATION = 30.0  # seconds a job runs in machine mode


class FakeRobot:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._job_start: float | None = None
        # Elapsed job time frozen by pause(); start() resumes from it. Mirrors the real
        # robot, where a paused job stays active (in_cleaning) until resumed or stopped.
        self._paused_elapsed: float | None = None
        self._carpet = CarpetMode.AVOID
        self._fan = FanPower.BALANCED
        self._mop = MopMode.STANDARD
        self._water = WaterMode.STANDARD
        self._cleaning = CleaningMode.VAC_AND_MOP
        self._battery = 100
        # Scripted mode for tests: (elapsed_since_t0, DeviceStatus), evaluated against _t0.
        self._timeline: list[tuple[float, DeviceStatus]] | None = None
        self._t0 = clock()
        # The route the current or last job drives, and how far along it a job that ended
        # early (stop/dock) got; the map draws the driven part as the trail.
        self._route: list[floor_plan.Pt] = [floor_plan.DOCK]
        self._route_mop = False
        self._route_avoids_carpet = True
        self._ended_at = 1.0
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
        # Settings readback mirrors the last set_* call, like the real status poll does.
        settings = dict(
            fan_power=self._fan.value, mop_route=self._mop.value, water_mode=self._water.value,
            cleaning_mode=self._cleaning.value, drying_mop=False
        )
        if self._paused_elapsed is not None:
            return DeviceStatus("paused", self._battery, in_cleaning=True, docked=False,
                                **settings)
        if self._job_start is None:
            return DeviceStatus("charging", self._battery, in_cleaning=False, docked=True,
                                **settings)
        elapsed = self._clock() - self._job_start
        if elapsed >= CLEAN_DURATION:
            self._job_start = None
            self._ended_at = 1.0
            return DeviceStatus("charging", self._battery, in_cleaning=False, docked=True,
                                **settings)
        if elapsed < DEPART_DELAY:
            # Job accepted but robot still on the dock: the "looks docked right after
            # trigger" case. in_cleaning is already true, which is what keeps the
            # completion predicate from firing early. Not yet out cleaning (cleaning=False).
            return DeviceStatus("charging", self._battery, in_cleaning=True, docked=True,
                                **settings)
        # Out cleaning: telemetry grows with elapsed time, so the UI shows live numbers.
        cleaning_for = elapsed - DEPART_DELAY
        return DeviceStatus(
            "cleaning", self._battery, in_cleaning=True, docked=False, cleaning=True,
            clean_area_m2=round(cleaning_for * 0.6, 1), clean_time_s=int(cleaning_for),
            **settings,
        )

    def map_ok(self) -> bool:
        return True

    async def get_map_png(self) -> bytes:
        trail, robot = self._position()
        return map_render.render(
            trail, robot, mop=self._route_mop,
            mop_lifted_on_carpet=not self._route_avoids_carpet,
        )

    async def get_map_data(self) -> MapData:
        robot = Point(*map_render.to_image(self._position()[1]))
        size = map_render.IMAGE_SIZE
        return MapData(rooms=list(_MAP_ROOMS), robot=robot, image_width=size, image_height=size)

    async def get_map_text(self) -> str:
        trail, robot = self._position()
        size = map_render.IMAGE_SIZE

        def norm(p: floor_plan.Pt) -> tuple[float, float]:
            x, y = map_render.to_image(p)
            return x / size, y / size

        rooms = [TextRoom(r.name, r.bbox) for r in _MAP_ROOMS if r.bbox]
        return render_map_text(rooms, norm(robot), [norm(p) for p in floor_plan.densify(trail)])

    def _progress(self) -> float:
        """Fraction of the route driven; the robot leaves after DEPART_DELAY and is home
        exactly at CLEAN_DURATION."""
        if self._paused_elapsed is not None:
            elapsed = self._paused_elapsed
        elif self._job_start is not None:
            elapsed = self._clock() - self._job_start
        else:
            return self._ended_at
        return (elapsed - DEPART_DELAY) / (CLEAN_DURATION - DEPART_DELAY)

    def _position(self) -> tuple[list[floor_plan.Pt], floor_plan.Pt]:
        trail, robot = floor_plan.walk(self._route, self._progress())
        if self._job_start is None and self._paused_elapsed is None:
            robot = floor_plan.DOCK  # stop/dock end the job with the robot home
        return trail, robot

    def _begin_route(self, segment_ids: list[int] | None) -> None:
        self._route = floor_plan.cleaning_route(
            segment_ids, avoid_carpet=self._carpet == CarpetMode.AVOID
        )
        self._route_mop = self._cleaning != CleaningMode.VACUUM
        self._route_avoids_carpet = self._carpet == CarpetMode.AVOID

    def _end_early(self) -> None:
        if self._job_start is not None or self._paused_elapsed is not None:
            self._ended_at = max(0.0, min(1.0, self._progress()))

    async def list_rooms(self) -> list[Room]:
        return list(_ROOMS)

    async def list_routines(self) -> list[Routine]:
        return list(_ROUTINES)

    async def get_last_clean_record(self) -> CleanRecord | None:
        return None

    async def get_consumables(self) -> Consumables:
        return Consumables(
            main_brush_pct=82, side_brush_pct=64, filter_pct=71,
            sensor_pct=45, strainer_pct=88, cleaning_brush_pct=93,
        )

    async def get_carpet_mode(self) -> CarpetMode:
        return self._carpet

    # --- onboarding -------------------------------------------------------
    async def list_devices(self) -> list[AccountDevice]:
        return [AccountDevice("fake-duid", "Fake Roborock", "roborock.vacuum.fake")]

    async def get_carpet_value(self) -> int:
        # Matches the fake-mode CARPET_RISE_VALUE / CARPET_AVOID_VALUE defaults.
        return {CarpetMode.RISE: 0, CarpetMode.AVOID: 1}[self._carpet]

    # --- commands ---------------------------------------------------------
    async def start(self) -> None:
        self.commands.append(("start",))
        if self._paused_elapsed is not None:
            self._job_start = self._clock() - self._paused_elapsed
            self._paused_elapsed = None
        else:
            self._begin_route(None)
            self._job_start = self._clock()

    async def pause(self) -> None:
        self.commands.append(("pause",))
        if self._job_start is not None:
            self._paused_elapsed = self._clock() - self._job_start
            self._job_start = None

    async def stop(self) -> None:
        self.commands.append(("stop",))
        self._end_early()
        self._job_start = None
        self._paused_elapsed = None

    async def dock(self) -> None:
        self.commands.append(("dock",))
        self._end_early()
        self._job_start = None
        self._paused_elapsed = None

    async def find_robot(self) -> None:
        self.commands.append(("find_robot",))

    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        # The default repeat is left out of the record so callers that never pass it
        # (the two-stage clean) keep asserting the historical 2-tuple shape.
        if repeat == 1:
            self.commands.append(("clean_rooms", tuple(segment_ids)))
        else:
            self.commands.append(("clean_rooms", tuple(segment_ids), repeat))
        self._begin_route(segment_ids)
        self._job_start = self._clock()

    async def reset_consumable(self, part: ConsumablePart) -> None:
        self.commands.append(("reset_consumable", part))

    async def set_fan_power(self, power: FanPower) -> None:
        self.commands.append(("set_fan_power", power))
        self._fan = power

    async def set_mop_mode(self, mode: MopMode) -> None:
        self.commands.append(("set_mop_mode", mode))
        self._mop = mode

    async def set_water_mode(self, mode: WaterMode) -> None:
        self.commands.append(("set_water_mode", mode))
        self._water = mode

    async def set_cleaning_mode(self, mode: CleaningMode) -> None:
        self.commands.append(("set_cleaning_mode", mode))
        self._cleaning = mode

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        self.commands.append(("set_carpet_mode", mode))
        self._carpet = mode

    async def run_routine(self, routine_id: int) -> None:
        self.commands.append(("run_routine", routine_id))
        self._begin_route(None)
        self._job_start = self._clock()
