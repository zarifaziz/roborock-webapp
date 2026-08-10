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
from roborock_auto.models import CarpetMode, DeviceStatus


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, dt: float) -> None:
        self.now += dt


def _config(**over) -> Config:
    base = dict(
        gateway="fake", email="", duid="", vacuum_scene_id=1001, mop_scene_id=1002,
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
    kinds = [c for c in gw.commands]
    assert ("set_carpet_mode", CarpetMode.RISE) in kinds
    assert ("run_routine", 1001) in kinds
    assert ("set_carpet_mode", CarpetMode.AVOID) in kinds
    assert ("run_routine", 1002) in kinds
    # Rise before Avoid, vacuum routine before mop routine.
    assert kinds.index(("set_carpet_mode", CarpetMode.RISE)) < kinds.index(
        ("set_carpet_mode", CarpetMode.AVOID)
    )
    assert kinds.index(("run_routine", 1001)) < kinds.index(("run_routine", 1002))


async def test_two_stage_aborts_if_not_docked_at_start():
    clock = FakeClock()
    gw = FakeGateway(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # already busy
    with pytest.raises(AutomationError, match="not docked"):
        await run_two_stage(
            gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )
