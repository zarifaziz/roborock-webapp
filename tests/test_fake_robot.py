"""P1: FakeRobot conforms to the full RobotGateway contract and is observation-free."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
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
from roborock_auto.ports import RobotGateway


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_satisfies_protocol():
    assert isinstance(FakeRobot(), RobotGateway)


async def test_reads_return_expected_types():
    gw = FakeRobot()
    assert isinstance(await gw.get_status(), DeviceStatus)
    assert (await gw.get_map_png())[:8] == b"\x89PNG\r\n\x1a\n"
    assert isinstance(await gw.get_map_data(), MapData)
    assert all(isinstance(r, Room) for r in await gw.list_rooms())
    assert all(isinstance(r, Routine) for r in await gw.list_routines())
    assert isinstance(await gw.get_consumables(), Consumables)
    assert isinstance(await gw.get_carpet_mode(), CarpetMode)


async def test_idle_when_no_job():
    status = await FakeRobot().get_status()
    assert status == DeviceStatus(
        "charging", 100, in_cleaning=False, docked=True,
        # defaults, readable before any set_*
        fan_power="balanced", mop_route="standard", water_mode="standard",
        cleaning_mode="vac_and_mop", drying_mop=False,
    )


async def test_status_reflects_setting_changes():
    """Settings readback: the status mirrors the last set_fan_power/set_mop_mode/set_water_mode."""
    gw = FakeRobot(clock=Clock())
    await gw.set_fan_power(FanPower.TURBO)
    await gw.set_mop_mode(MopMode.DEEP)
    await gw.set_water_mode(WaterMode.INTENSE)
    status = await gw.get_status()
    assert (status.fan_power, status.mop_route, status.water_mode) == ("turbo", "deep", "intense")


@pytest.mark.parametrize(
    "mode, expected",
    [
        (CleaningMode.VACUUM, ("balanced", None, "standard")),
        (CleaningMode.VAC_AND_MOP, ("balanced", "standard", "standard")),
        (CleaningMode.MOP, ("off", "standard", "standard")),
    ],
)
async def test_mode_switch_overwrites_fan_water_and_route_like_the_library(mode, expected):
    """python-roborock's SET_CLEAN_MOTOR_MODE carries fan, water and route together, so a
    mode switch on the real robot discards what Setup chose. The fake must do the same or
    no test can see that loss."""
    gw = FakeRobot(clock=Clock())
    await gw.set_fan_power(FanPower.TURBO)
    await gw.set_mop_mode(MopMode.DEEP)
    await gw.set_water_mode(WaterMode.INTENSE)
    await gw.set_cleaning_mode(mode)
    status = await gw.get_status()
    assert (status.fan_power, status.water_mode, status.mop_route) == expected


async def test_cleaning_status_carries_telemetry():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.start()
    clock.now = 13.0  # 3s depart delay + 10s of actual cleaning
    assert await gw.get_status() == DeviceStatus(
        "cleaning", 100, in_cleaning=True, docked=False, cleaning=True,
        fan_power="balanced", mop_route="standard", water_mode="standard",
        cleaning_mode="vac_and_mop", drying_mop=False,
        clean_area_m2=6.0, clean_time_s=10,
    )


async def test_polling_does_not_advance_progress():
    """Same clock time -> same status, no matter how many times we poll."""
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.start()
    clock.now = 10.0  # inside the cleaning window
    first = await gw.get_status()
    for _ in range(5):
        assert await gw.get_status() == first
    assert first.in_cleaning and not first.docked


async def test_machine_lifecycle_docked_cleaning_docked():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.start()
    clock.now = 1.0
    assert (await gw.get_status()).docked            # lingering on dock, in_cleaning true
    assert (await gw.get_status()).in_cleaning
    clock.now = 10.0
    assert not (await gw.get_status()).docked        # out cleaning
    clock.now = 40.0
    done = await gw.get_status()
    assert done.docked and not done.in_cleaning       # back home, job over


async def test_commands_recorded_and_carpet_roundtrip():
    gw = FakeRobot()
    await gw.set_carpet_mode(CarpetMode.RISE)
    assert await gw.get_carpet_mode() == CarpetMode.RISE
    await gw.set_fan_power(FanPower.TURBO)
    await gw.set_mop_mode(MopMode.DEEP)
    await gw.run_routine(1001)
    assert ("set_carpet_mode", CarpetMode.RISE) in gw.commands
    assert ("run_routine", 1001) in gw.commands


async def test_stop_ends_job():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.start()
    clock.now = 10.0
    assert (await gw.get_status()).in_cleaning
    await gw.stop()
    done = await gw.get_status()
    assert done.docked and not done.in_cleaning
    assert ("stop",) in gw.commands


async def test_find_robot_records_without_state_change():
    gw = FakeRobot()
    before = await gw.get_status()
    await gw.find_robot()
    assert await gw.get_status() == before
    assert gw.commands == [("find_robot",)]


async def test_timeline_is_clock_relative():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    a = DeviceStatus("cleaning", 90, in_cleaning=True, docked=False)
    b = DeviceStatus("charging", 88, in_cleaning=False, docked=True)
    gw.set_timeline([(0, a), (5, b)])
    assert await gw.get_status() == a
    clock.now = 5.0
    assert await gw.get_status() == b
    clock.now = 100.0
    assert await gw.get_status() == b


# --- the demo floor plan ------------------------------------------------------
# The fake map is a 92 x 92 floor plan in 10 cm units, drawn with a 3-unit margin at 10 px
# per unit, so the PNG is 980 px square.
_PX_PER_UNIT = 10
_MARGIN = 3
_SIZE = 980


def _box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
    """Plan-unit room bounds -> the normalized bbox the click overlay uses."""
    return tuple(round((v + _MARGIN) * _PX_PER_UNIT / _SIZE, 4) for v in (x0, y0, x1, y1))


def _px(x: float, y: float) -> Point:
    return Point(int((x + _MARGIN) * _PX_PER_UNIT), int((y + _MARGIN) * _PX_PER_UNIT))


_FLOOR_PLAN_ROOMS = [
    Room(16, "Living Room", bbox=_box(0, 48, 58, 92), color="#16d5cb"),
    Room(17, "Kitchen", bbox=_box(58, 48, 92, 92), color="#ffaf97"),
    Room(18, "Bedroom", bbox=_box(0, 0, 40, 36), color="#9ccaff"),
    Room(19, "Hallway", bbox=_box(14, 36, 92, 48), color="#ffdd5e"),
    Room(20, "Study", bbox=_box(60, 0, 92, 36), color="#9ccaff"),
    Room(21, "Bathroom", bbox=_box(40, 0, 60, 22), color="#16d5cb"),
    Room(22, "Laundry", bbox=_box(40, 22, 60, 36), color="#ffaf97"),
]
_DOCKED = _px(44, 89.5)


def _inside(point: Point, room: Room) -> bool:
    x0, y0, x1, y1 = room.bbox
    return x0 * _SIZE <= point.x <= x1 * _SIZE and y0 * _SIZE <= point.y <= y1 * _SIZE


async def test_map_data_serves_the_floor_plan_with_the_robot_on_its_dock():
    assert await FakeRobot().get_map_data() == MapData(
        rooms=_FLOOR_PLAN_ROOMS, robot=_DOCKED, image_width=_SIZE, image_height=_SIZE,
    )


async def test_list_rooms_names_the_same_rooms_as_the_map():
    assert await FakeRobot().list_rooms() == [
        Room(r.segment_id, r.name) for r in _FLOOR_PLAN_ROOMS
    ]


async def test_map_png_is_high_resolution_and_matches_map_data():
    png = await FakeRobot().get_map_png()
    assert Image.open(io.BytesIO(png)).size == (_SIZE, _SIZE)


async def test_robot_drives_through_the_selected_room_and_returns_to_the_dock():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    study = _FLOOR_PLAN_ROOMS[4]
    await gw.clean_rooms([study.segment_id])

    async def robot_at(t: float) -> Point:
        clock.now = t
        return (await gw.get_map_data()).robot

    assert await robot_at(1.0) == _DOCKED          # still lingering on the dock
    mid = [await robot_at(t) for t in (14.0, 16.0, 18.0)]
    assert all(_inside(p, study) for p in mid)
    assert len(set(mid)) == 3                       # moving, not parked
    assert await robot_at(29.9) != _DOCKED          # still on the way home
    assert await robot_at(31.0) == _DOCKED


async def test_paused_robot_holds_its_position():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.clean_rooms([20])
    clock.now = 15.0
    await gw.pause()
    paused_at = (await gw.get_map_data()).robot
    clock.now = 25.0
    assert (await gw.get_map_data()).robot == paused_at != _DOCKED


async def test_trail_grows_through_the_cleaned_room_only():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.clean_rooms([20])

    async def coverage(t: float) -> dict[str, int]:
        clock.now = t
        return _coverage(await gw.get_map_text())

    early, late = await coverage(12.0), await coverage(20.0)
    assert 0 < early["Study"] < late["Study"]
    assert early["Bedroom"] == late["Bedroom"] == 0
    assert early["Kitchen"] == late["Kitchen"] == 0


async def test_png_draws_the_robot_where_map_data_puts_it():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.clean_rooms([20])
    clock.now = 15.0
    robot = (await gw.get_map_data()).robot
    image = Image.open(io.BytesIO(await gw.get_map_png())).convert("RGB")
    assert image.getpixel((robot.x, robot.y)) == (10, 132, 255)


async def test_png_shows_motion_and_mop_coverage():
    clock = Clock()
    vacuum, mop = FakeRobot(clock=clock), FakeRobot(clock=clock)
    await mop.set_cleaning_mode(CleaningMode.MOP)
    await vacuum.set_cleaning_mode(CleaningMode.VACUUM)
    for gw in (vacuum, mop):
        await gw.clean_rooms([20])
    docked_png = await vacuum.get_map_png()
    clock.now = 18.0
    assert await vacuum.get_map_png() != docked_png
    assert await mop.get_map_png() != await vacuum.get_map_png()


def _coverage(map_text: str) -> dict[str, int]:
    """Legend lines read '  A  Study — 37% covered'."""
    rows = [line.split(None, 1)[1] for line in map_text.splitlines() if "% covered" in line]
    return {name: int(pct.split("%")[0]) for name, pct in (r.split(" — ") for r in rows)}


async def test_stopping_sends_the_robot_home_and_keeps_the_trail_so_far():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.clean_rooms([20])
    clock.now = 18.0
    driven = _coverage(await gw.get_map_text())["Study"]
    await gw.stop()
    clock.now = 25.0
    assert (await gw.get_map_data()).robot == _DOCKED
    assert _coverage(await gw.get_map_text())["Study"] == driven > 0
