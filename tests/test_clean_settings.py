"""Fan power, water flow and mop route survive the cleaning-mode switch a clean starts with.

The switch resets all three on the real robot (and in the honest FakeRobot), so every
start path has to put them back before the robot leaves the dock.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.adapters.sqlite import SqliteCleaningLog, open_database
from roborock_auto.application.sequence_runner import run_sequence
from roborock_auto.application.start_clean import start_clean
from roborock_auto.config import Config
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleanSettings,
    CleanSource,
    FanPower,
    MopMode,
    Step,
    WaterMode,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, dt: float) -> None:
        self.now += dt


class RecordingRobot(FakeRobot):
    """Notes (fan power, water flow, mop route) at the moment each clean is triggered."""

    def __init__(self, clock) -> None:
        super().__init__(clock=clock)
        self.settings_at_departure: list[tuple[str | None, str | None, str | None]] = []

    async def _note(self) -> None:
        status = await self.get_status()
        self.settings_at_departure.append((status.fan_power, status.water_mode, status.mop_route))

    async def start(self) -> None:
        await self._note()
        await super().start()

    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        await self._note()
        await super().clean_rooms(segment_ids, repeat)


def _config() -> Config:
    return Config(
        gateway="fake",
        email="",
        duid="",
        carpet_rise_value=0,
        carpet_avoid_value=1,
        poll_interval_sec=5.0,
        app_port=8000,
        session_path="user_data.json",
        database_path=":memory:",
        timezone="Australia/Sydney",
    )


async def _robot_with_setup(clock) -> RecordingRobot:
    """A robot whose Setup tab says Turbo, Intense water, Deep route."""
    gw = RecordingRobot(clock)
    await gw.set_fan_power(FanPower.TURBO)
    await gw.set_water_mode(WaterMode.INTENSE)
    await gw.set_mop_mode(MopMode.DEEP)
    gw.commands.clear()
    return gw


async def _run(gw: RecordingRobot, clock: FakeClock, steps: tuple[Step, ...]) -> None:
    await run_sequence(
        gw,
        steps,
        _config(),
        cleaning_log=SqliteCleaningLog(open_database(":memory:"), device_id="fake"),
        source=CleanSource.APP,
        clock=clock,
        sleep=clock.sleep,
        interval=5,
        log=lambda _m: None,
    )


def step(mode: CleaningMode, settings: CleanSettings = CleanSettings()) -> Step:
    return Step(mode=mode, carpet=CarpetMode.AVOID, segment_ids=None, settings=settings)


async def test_sequence_steps_keep_the_setup_fan_water_and_route():
    clock = FakeClock()
    gw = await _robot_with_setup(clock)
    await _run(
        gw,
        clock,
        (
            step(CleaningMode.VACUUM),
            step(CleaningMode.VAC_AND_MOP),
            step(CleaningMode.MOP),
        ),
    )
    assert gw.settings_at_departure == [
        ("turbo", None, "standard"),  # vacuum only: water stays off, route not mopping
        ("turbo", "intense", "deep"),
        ("off", "intense", "deep"),  # mop only: suction stays off
    ]


async def test_a_step_setting_wins_over_setup_field_by_field():
    clock = FakeClock()
    gw = await _robot_with_setup(clock)
    await _run(
        gw,
        clock,
        (
            step(CleaningMode.VACUUM, settings=CleanSettings(fan_power=FanPower.MAX)),
            step(CleaningMode.VAC_AND_MOP, settings=CleanSettings(water_mode=WaterMode.MILD)),
            step(CleaningMode.MOP, settings=CleanSettings(mop_route=MopMode.STANDARD)),
        ),
    )
    assert gw.settings_at_departure == [
        ("max", None, "standard"),
        ("turbo", "mild", "deep"),
        ("off", "intense", "standard"),
    ]


async def test_setup_is_read_once_so_a_vacuum_step_does_not_dry_out_the_next_mop():
    clock = FakeClock()
    gw = await _robot_with_setup(clock)
    await _run(gw, clock, (step(CleaningMode.VACUUM), step(CleaningMode.MOP)))
    assert gw.commands == [
        ("set_carpet_mode", CarpetMode.AVOID),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_fan_power", FanPower.TURBO),
        ("start",),
        ("set_carpet_mode", CarpetMode.AVOID),
        ("set_cleaning_mode", CleaningMode.MOP),
        ("set_water_mode", WaterMode.INTENSE),
        ("set_mop_mode", MopMode.DEEP),
        ("start",),
    ]


@pytest.mark.parametrize(
    "mode, expected",
    [
        (CleaningMode.VACUUM, ("turbo", None, "standard")),
        (CleaningMode.VAC_AND_MOP, ("turbo", "intense", "deep")),
        (CleaningMode.MOP, ("off", "intense", "deep")),
    ],
)
async def test_clean_tab_start_keeps_the_setup_fan_water_and_route(mode, expected):
    clock = FakeClock()
    gw = await _robot_with_setup(clock)
    await start_clean(
        gw,
        cleaning_log=AsyncMock(),
        source=CleanSource.APP,
        mode=mode,
        segment_ids=None,
    )
    assert gw.settings_at_departure == [expected]


class FanRefusedRobot(RecordingRobot):
    async def set_fan_power(self, power: FanPower) -> None:
        raise ConnectionError("refused")


async def test_a_setting_the_robot_refuses_does_not_stop_the_sequence():
    clock = FakeClock()
    gw = FanRefusedRobot(clock)
    await gw.set_water_mode(WaterMode.INTENSE)
    await gw.set_mop_mode(MopMode.DEEP)
    gw.commands.clear()
    log: list[str] = []
    await run_sequence(
        gw,
        (step(CleaningMode.VAC_AND_MOP, settings=CleanSettings(fan_power=FanPower.TURBO)),),
        _config(),
        cleaning_log=SqliteCleaningLog(open_database(":memory:"), device_id="fake"),
        source=CleanSource.APP,
        clock=clock,
        sleep=clock.sleep,
        interval=5,
        log=log.append,
    )
    assert gw.settings_at_departure == [("balanced", "intense", "deep")]
    assert [line for line in log if "could not" in line] == [
        "step 1/1: could not set fan power to turbo, cleaning with the mode's default: refused"
    ]


async def test_a_setting_the_robot_refuses_does_not_stop_a_plain_clean(caplog):
    clock = FakeClock()
    gw = FanRefusedRobot(clock)
    with caplog.at_level(logging.WARNING, logger="roborock_auto.application.start_clean"):
        await start_clean(
            gw,
            cleaning_log=AsyncMock(),
            source=CleanSource.APP,
            mode=CleaningMode.VACUUM,
            segment_ids=None,
        )
    assert gw.settings_at_departure == [("balanced", None, "standard")]
    assert [r.getMessage() for r in caplog.records] == [
        "could not set fan power to balanced, cleaning with the mode's default: refused"
    ]
