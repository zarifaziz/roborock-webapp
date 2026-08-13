"""P2: the two-stage sequence and the depart-then-return poll."""

from __future__ import annotations

import pytest

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.application.retry import AutomationError
from roborock_auto.application.two_stage_clean import (
    run_two_stage,
    wait_for_departure,
    wait_for_return,
)
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, CleaningMode, DeviceStatus


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
PAUSED = DeviceStatus("paused", 80, in_cleaning=True, docked=False)
CANCELLED_OFF_DOCK = DeviceStatus("idle", 80, in_cleaning=False, docked=False)


async def test_departure_returns_when_job_active():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE), (5, CLEANING)])
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=180)
    assert clock.now == 5.0


async def test_departure_times_out_if_never_starts():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE)])  # never leaves
    with pytest.raises(AutomationError, match="never started"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_return_ignores_midjob_recharge():
    """The core correctness case: a charge blip mid-job must NOT count as done."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
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
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING), (5, ERRORED)])
    with pytest.raises(AutomationError, match="brush stuck"):
        await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_aborts_after_persistent_pause():
    """A pause nobody resumes fails in stall_timeout, not the full 2h return timeout."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING), (10, PAUSED)])
    with pytest.raises(AutomationError, match="stalled"):
        await wait_for_return(
            gw, clock=clock, sleep=clock.sleep, interval=5, timeout=7200, stall_timeout=60
        )
    assert clock.now < 120  # aborted shortly after the 60s stall window, not at 7200


async def test_return_survives_brief_pause():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([
        (0, CLEANING),
        (10, PAUSED),        # paused briefly...
        (30, CLEANING),      # ...then resumed before the stall window expires
        (40, DOCKED_IDLE),
    ])
    await wait_for_return(
        gw, clock=clock, sleep=clock.sleep, interval=5, timeout=7200, stall_timeout=60
    )
    assert clock.now == 40.0


async def test_return_aborts_when_cancelled_off_dock():
    """A user cancel that leaves the robot idle off-dock is a stall, not a 2h wait."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING), (10, CANCELLED_OFF_DOCK)])
    with pytest.raises(AutomationError, match="stalled"):
        await wait_for_return(
            gw, clock=clock, sleep=clock.sleep, interval=5, timeout=7200, stall_timeout=60
        )


async def test_return_times_out_when_stuck():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # never returns
    with pytest.raises(AutomationError, match="timed out"):
        await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_two_stage_happy_path_ordering():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)  # machine mode: docked -> cleaning -> docked
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


async def test_two_stage_reports_stage_sequence():
    """on_stage fires the exact stage tokens, in order, the status endpoint reports."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    stages: list[str] = []
    await run_two_stage(
        gw, _config(), clock=clock, sleep=clock.sleep, interval=5,
        log=lambda m: None, on_stage=stages.append,
    )
    assert stages == ["vacuuming", "vacuum_waiting", "mopping", "mop_waiting", "done"]


async def test_two_stage_scoped_departs_with_clean_rooms():
    """Room-scoped run departs each stage with clean_rooms(segments), never bare start()."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    await run_two_stage(
        gw, _config(), clock=clock, sleep=clock.sleep, interval=5,
        segment_ids=[16, 17], log=lambda m: None,
    )
    departs = [c for c in gw.commands if c[0] in ("start", "clean_rooms")]
    assert departs == [("clean_rooms", (16, 17)), ("clean_rooms", (16, 17))]


async def test_two_stage_aborts_if_carpet_mode_does_not_take():
    """Safety: if the carpet write silently fails, abort before starting the clean."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)

    async def stuck_carpet(mode):
        gw.commands.append(("set_carpet_mode", mode))  # pretend to accept but do not change

    gw.set_carpet_mode = stuck_carpet  # get_carpet_mode still returns the old AVOID
    with pytest.raises(AutomationError, match="did not take"):
        await run_two_stage(
            gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )


async def test_two_stage_aborts_if_not_docked_at_start():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # already busy
    with pytest.raises(AutomationError, match="not docked"):
        await run_two_stage(
            gw, _config(), clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )
