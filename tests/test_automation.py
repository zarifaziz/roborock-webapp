"""P2: the two-stage sequence and the depart-then-return poll."""

from __future__ import annotations

import pytest

from roborock_auto.automation import (
    AutomationError,
    run_two_stage,
    wait_for_departure,
    wait_for_return,
)
from roborock_auto.config import Config
from roborock_auto.gateway.fake import FakeGateway
from roborock_auto.models import CarpetMode, CleaningMode, DeviceStatus


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, dt: float) -> None:
        self.now += dt


def _config(**over) -> Config:
    base = dict(
        gateway="fake", email="", duid="",
        carpet_rise_value=0, carpet_avoid_value=1, poll_interval_sec=5.0, app_port=8000,
        session_path="user_data.json",
    )
    base.update(over)
    return Config(**base)


CLEANING = DeviceStatus("cleaning", 90, in_cleaning=True, docked=False)
DOCKED_IDLE = DeviceStatus("charging", 100, in_cleaning=False, docked=True)
RECHARGING_MIDJOB = DeviceStatus("charging", 20, in_cleaning=True, docked=True)
ERRORED = DeviceStatus("error", 50, in_cleaning=False, docked=False, error="brush stuck")


async def test_departure_returns_when_job_active():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE), (5, CLEANING)])
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=180)
    assert clock.now == 5.0


async def test_departure_times_out_if_never_starts():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE)])  # never leaves
    with pytest.raises(AutomationError, match="never started"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_return_ignores_midjob_recharge():
    """The core correctness case: a charge blip mid-job must NOT count as done."""
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([
        (0, CLEANING),
        (10, RECHARGING_MIDJOB),   # docked AND charging, but in_cleaning still true
        (20, CLEANING),            # resumed
        (30, DOCKED_IDLE),         # genuinely done
    ])
    await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)
    assert clock.now == 30.0  # only stopped at the real completion, not the recharge blip


async def test_return_raises_on_error():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, CLEANING), (5, ERRORED)])
    with pytest.raises(AutomationError, match="brush stuck"):
        await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_times_out_when_stuck():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # never returns
    with pytest.raises(AutomationError, match="timed out"):
        await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_two_stage_happy_path_ordering():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)  # machine mode: docked -> cleaning -> docked
    await run_two_stage(
        gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
    )
    kinds = gw.commands
    assert ("set_carpet_mode", CarpetMode.RISE) in kinds
    assert ("set_cleaning_mode", CleaningMode.VACUUM) in kinds
    assert ("set_carpet_mode", CarpetMode.AVOID) in kinds
    assert ("set_cleaning_mode", CleaningMode.MOP) in kinds
    # Vacuum stage fully before mop stage: Rise+vacuum both precede Avoid+mop.
    assert kinds.index(("set_carpet_mode", CarpetMode.RISE)) < kinds.index(
        ("set_carpet_mode", CarpetMode.AVOID)
    )
    assert kinds.index(("set_cleaning_mode", CleaningMode.VACUUM)) < kinds.index(
        ("set_cleaning_mode", CleaningMode.MOP)
    )
    # Each stage sets carpet+mode before it starts.
    assert kinds.index(("set_cleaning_mode", CleaningMode.VACUUM)) < kinds.index(("start",))


async def test_two_stage_aborts_if_carpet_mode_does_not_take():
    """Safety: if the carpet write silently fails, abort before starting the clean."""
    clock = FakeClock()
    gw = FakeGateway(clock=clock)

    async def stuck_carpet(mode):
        gw.commands.append(("set_carpet_mode", mode))  # pretend to accept but do not change

    gw.set_carpet_mode = stuck_carpet  # get_carpet_mode still returns the old AVOID
    with pytest.raises(AutomationError, match="did not take"):
        await run_two_stage(
            gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )


async def test_two_stage_aborts_if_not_docked_at_start():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # already busy
    with pytest.raises(AutomationError, match="not docked"):
        await run_two_stage(
            gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )
