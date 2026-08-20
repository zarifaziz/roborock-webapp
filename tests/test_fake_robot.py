"""P1: FakeRobot conforms to the full RobotGateway contract and is observation-free."""

from __future__ import annotations

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.domain.models import (
    CarpetMode,
    Consumables,
    DeviceStatus,
    FanPower,
    MapData,
    MopMode,
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
    )


async def test_status_reflects_setting_changes():
    """Settings readback: the status mirrors the last set_fan_power/set_mop_mode/set_water_mode."""
    gw = FakeRobot(clock=Clock())
    await gw.set_fan_power(FanPower.TURBO)
    await gw.set_mop_mode(MopMode.DEEP)
    await gw.set_water_mode(WaterMode.INTENSE)
    status = await gw.get_status()
    assert (status.fan_power, status.mop_route, status.water_mode) == ("turbo", "deep", "intense")


async def test_cleaning_status_carries_telemetry():
    clock = Clock()
    gw = FakeRobot(clock=clock)
    await gw.start()
    clock.now = 13.0  # 3s depart delay + 10s of actual cleaning
    assert await gw.get_status() == DeviceStatus(
        "cleaning", 100, in_cleaning=True, docked=False, cleaning=True,
        fan_power="balanced", mop_route="standard", water_mode="standard",
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
