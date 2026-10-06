"""P2: the two-stage sequence and the depart-then-return poll."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.adapters.sqlite import SqliteCleaningLog, open_database
from roborock_auto.application.retry import AutomationError
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.sequence_runner import Phase, run_sequence
from roborock_auto.application.two_stage_clean import (
    STATUS_FAULT_BUDGET,
    RunCancelled,
    run_two_stage,
    wait_for_departure,
    wait_for_return,
)
from roborock_auto.config import Config
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleaningRun,
    CleanSource,
    DeviceStatus,
    Step,
)


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
        session_path="user_data.json", database_path=":memory:", timezone="Australia/Sydney",
    )
    base.update(over)
    return Config(**base)


def _log() -> SqliteCleaningLog:
    return SqliteCleaningLog(open_database(":memory:"), device_id="fake")


CLEANING = DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True)
DOCKED_IDLE = DeviceStatus("charging", 100, in_cleaning=False, docked=True)
RECHARGING_MIDJOB = DeviceStatus("charging", 20, in_cleaning=True, docked=True)
ERRORED = DeviceStatus("error", 50, in_cleaning=False, docked=False, error="brush stuck")
PAUSED = DeviceStatus("paused", 80, in_cleaning=True, docked=False)
CANCELLED_OFF_DOCK = DeviceStatus("idle", 80, in_cleaning=False, docked=False)
# Post-clean dock servicing (empty the bin / wash the mop). in_cleaning is still true and the
# robot is off the charger, yet it has NOT left to clean — it is finishing the previous stage.
SERVICING_AT_DOCK = DeviceStatus("emptying_the_bin", 95, in_cleaning=True, docked=False)


async def test_departure_returns_when_job_active():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE), (5, CLEANING)])
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=180)
    assert clock.now == 10.0


async def test_departure_ignores_dock_servicing_from_prior_stage():
    """Regression: the mop stage fired while the robot was still emptying the bin from the
    vacuum stage. That servicing is in_cleaning=True but is NOT the robot leaving to mop, so
    treating it as departure let the stage 'complete' with no mopping. A robot that only
    services then settles idle never departed — that must raise, not silently succeed."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, SERVICING_AT_DOCK), (5, DOCKED_IDLE)])  # services, docks, never leaves
    with pytest.raises(AutomationError, match="never started"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_departure_ignores_a_single_stale_cleaning_sample():
    """A stale cached 'cleaning' read from the just-finished vacuum must not confirm the mop
    departure; a real departure shows cleaning across consecutive polls. Physically a robot
    cannot be out cleaning one poll and idle-docked the next."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING), (5, DOCKED_IDLE)])  # one cleaning sample, then idle
    with pytest.raises(AutomationError, match="did not take"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5)


async def test_departure_times_out_if_never_starts():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE)])  # never leaves
    with pytest.raises(AutomationError, match="never started"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=30)


async def test_departure_waits_through_long_dock_servicing():
    """Before a mop, the robot washes the mop for minutes. That dock-servicing must not trip
    the departure wait — only leaving to clean (or settling idle) ends it. On live the wash
    ran ~168s, just under the old fixed 180s timeout; a longer wash would falsely fail."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    washing = DeviceStatus("washing_the_mop", 95, in_cleaning=True, docked=False)
    gw.set_timeline([(0, washing), (300, CLEANING)])  # 5 min washing, then departs to clean
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5)
    assert clock.now >= 300.0


async def test_departure_fails_fast_when_robot_settles_idle():
    """A dropped trigger leaves the robot idle on the dock. Fail within the idle grace, not
    after the long servicing backstop, so the caller can re-issue the command."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, DOCKED_IDLE)])  # settled idle, never departs, never services
    with pytest.raises(AutomationError, match="did not take"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5)
    assert clock.now < 120.0


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
    assert clock.now == 35.0  # confirmed at the real completion, not the recharge blip


async def test_return_ignores_a_single_idle_flicker():
    """One home_and_idle poll during a mid-job recharge (an in_cleaning flag glitch) must not
    be read as done, or the mop stage launches into a still-running vacuum."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([
        (0, CLEANING),
        (10, RECHARGING_MIDJOB),
        (15, DOCKED_IDLE),        # single-poll flag glitch reads as done
        (20, RECHARGING_MIDJOB),  # corrects itself
        (40, DOCKED_IDLE),        # genuinely done
    ])
    await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)
    assert clock.now == 45.0  # confirmed one poll after the sustained idle, not at the glitch


async def test_return_ignores_a_single_error_blip():
    """A one-poll self-clearing error (robot nudges a rug edge, recovers) must not abort a
    healthy unattended run; a real error persists across polls."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([
        (0, CLEANING),
        (5, ERRORED),      # one-poll blip
        (10, CLEANING),    # recovered
        (30, DOCKED_IDLE),
    ])
    await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)
    assert clock.now == 35.0


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
    assert clock.now == 45.0  # completion confirmed one poll after the sustained idle


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


class _FlakyStatusRobot(FakeRobot):
    """Machine/timeline robot whose get_status raises N times before recovering, mimicking a
    cloud/MQTT blip. One raised poll must not abort an hours-long unattended run."""

    def __init__(self, clock, failures: int) -> None:
        super().__init__(clock=clock)
        self._failures_left = failures

    async def get_status(self) -> DeviceStatus:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise ConnectionError("mqtt blip")
        return await super().get_status()


async def test_departure_tolerates_status_faults_within_budget():
    clock = FakeClock()
    gw = _FlakyStatusRobot(clock=clock, failures=STATUS_FAULT_BUDGET)
    gw.set_timeline([(0, DOCKED_IDLE), (5, CLEANING)])
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_departure_raises_lost_contact_past_fault_budget():
    clock = FakeClock()
    gw = _FlakyStatusRobot(clock=clock, failures=STATUS_FAULT_BUDGET + 1)
    gw.set_timeline([(0, DOCKED_IDLE), (5, CLEANING)])
    with pytest.raises(AutomationError, match="lost contact"):
        await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_tolerates_status_faults_within_budget():
    clock = FakeClock()
    gw = _FlakyStatusRobot(clock=clock, failures=STATUS_FAULT_BUDGET)
    gw.set_timeline([(0, CLEANING), (40, DOCKED_IDLE)])
    await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_raises_lost_contact_past_fault_budget():
    clock = FakeClock()
    gw = _FlakyStatusRobot(clock=clock, failures=STATUS_FAULT_BUDGET + 1)
    gw.set_timeline([(0, CLEANING), (40, DOCKED_IDLE)])
    with pytest.raises(AutomationError, match="lost contact"):
        await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_fault_counter_resets_on_success():
    """Failures are budgeted per consecutive streak, not per run: two separate blips shorter
    than the budget must not add up to an abort."""
    clock = FakeClock()

    class _TwoBlipsRobot(FakeRobot):
        def __init__(self, clock) -> None:
            super().__init__(clock=clock)
            self.calls = 0

        async def get_status(self) -> DeviceStatus:
            self.calls += 1
            # Two separate blips of `budget` failures each, split by one good poll.
            budget = STATUS_FAULT_BUDGET
            if self.calls <= budget or budget + 1 < self.calls <= 2 * budget + 1:
                raise ConnectionError("mqtt blip")
            return await super().get_status()

    gw = _TwoBlipsRobot(clock=clock)
    gw.set_timeline([(0, CLEANING), (80, DOCKED_IDLE)])
    await wait_for_return(gw, clock=clock, sleep=clock.sleep, interval=5, timeout=600)


async def test_return_survives_long_offline_blip():
    """A >stall_timeout stretch of unknown/device_offline is 'no information', not a stall.
    The run continues and completes once the robot reports in again."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    offline = DeviceStatus("device_offline", 80, in_cleaning=False, docked=False)
    gw.set_timeline([
        (0, CLEANING),
        (10, offline),         # 7 min of no information — longer than the stall window
        (430, CLEANING),
        (450, DOCKED_IDLE),
    ])
    await wait_for_return(
        gw, clock=clock, sleep=clock.sleep, interval=5, timeout=7200, stall_timeout=300
    )
    assert clock.now == 455.0


async def test_departure_waits_through_charge_before_depart():
    """A deep-discharged robot accepts the clean but charges before leaving (docked AND
    in_cleaning). That is progress, so it must extend the departure deadline — a >30 min
    charge must not abort a job that is actually pending."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, RECHARGING_MIDJOB), (2400, CLEANING)])  # 40 min charge > DEPART_TIMEOUT
    await wait_for_departure(gw, clock=clock, sleep=clock.sleep, interval=5)
    assert clock.now >= 2400.0


async def test_two_stage_happy_path_ordering():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)  # machine mode: docked -> cleaning -> docked
    await run_two_stage(
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
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
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5,
        log=lambda m: None, on_stage=stages.append,
    )
    assert stages == ["vacuuming", "vacuum_waiting", "mopping", "mop_waiting", "done"]


async def test_two_stage_scoped_departs_with_clean_rooms():
    """Room-scoped run departs each stage with clean_rooms(segments), never bare start()."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    await run_two_stage(
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5,
        segment_ids=[16, 17], log=lambda m: None,
    )
    departs = [c for c in gw.commands if c[0] in ("start", "clean_rooms")]
    assert departs == [("clean_rooms", (16, 17)), ("clean_rooms", (16, 17))]


class _DropFirstDepartRobot(FakeRobot):
    """Machine-mode robot that silently ignores its first N depart commands, mimicking the
    firmware dropping a clean sent while the robot is still servicing the dock."""

    def __init__(self, clock, drops: int) -> None:
        super().__init__(clock=clock)
        self._drops_left = drops

    async def clean_rooms(self, segment_ids, repeat=1):
        self.commands.append(("clean_rooms", tuple(segment_ids)))
        if self._drops_left > 0:
            self._drops_left -= 1
            return  # dropped: no job starts, robot stays idle on the dock
        self._job_start = self._clock()


async def test_two_stage_retries_a_dropped_depart():
    """The mop that 'didn't happen' live: the depart was dropped because the robot was still
    servicing. A dropped depart leaves the robot idle, so it must be re-issued, not treated
    as a failure or a completed stage."""
    clock = FakeClock()
    gw = _DropFirstDepartRobot(clock=clock, drops=1)  # drop only the vacuum stage's first try
    await run_two_stage(
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5, segment_ids=[7],
        log=lambda m: None,
    )
    departs = [c for c in gw.commands if c[0] == "clean_rooms"]
    # vacuum: dropped once then re-issued (2), mop: once (1).
    assert departs == [("clean_rooms", (7,)), ("clean_rooms", (7,)), ("clean_rooms", (7,))]


async def test_two_stage_aborts_if_carpet_mode_does_not_take():
    """Safety: if the carpet write silently fails, abort before starting the clean."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)

    async def stuck_carpet(mode):
        gw.commands.append(("set_carpet_mode", mode))  # pretend to accept but do not change

    gw.set_carpet_mode = stuck_carpet  # get_carpet_mode still returns the old AVOID
    with pytest.raises(AutomationError, match="did not take"):
        await run_two_stage(
            gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )


async def test_two_stage_rejects_a_coarse_poll_interval():
    """A poll interval near the idle grace would fail-fast on 'trigger did not take' after
    just a sample or two and re-issue a clean the robot actually accepted. Reject it early."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    with pytest.raises(AutomationError, match="poll interval"):
        await run_two_stage(
            gw, _config(poll_interval_sec=30.0), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep,
            log=lambda m: None,
        )


async def test_two_stage_aborts_if_not_docked_at_start():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, CLEANING)])  # already busy
    with pytest.raises(AutomationError, match="not docked"):
        await run_two_stage(
            gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )


class _ErrorsMidCleanRobot(FakeRobot):
    """Machine-mode robot that reports a persistent device error once it is out cleaning."""

    async def get_status(self) -> DeviceStatus:
        status = await super().get_status()
        return ERRORED if status.cleaning else status


async def test_two_stage_docks_and_restores_carpet_on_abort():
    """An abort mid-run must not leave the robot mid-floor with wet pads down and carpet mode
    latched at Rise: best-effort dock + carpet Avoid, then re-raise the original error."""
    clock = FakeClock()
    gw = _ErrorsMidCleanRobot(clock=clock)
    with pytest.raises(AutomationError, match="brush stuck"):
        await run_two_stage(
            gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )
    assert ("dock",) in gw.commands
    # The recovery Avoid comes after the vacuum stage's Rise, and is the final carpet write.
    carpet_writes = [c for c in gw.commands if c[0] == "set_carpet_mode"]
    assert carpet_writes == [
        ("set_carpet_mode", CarpetMode.RISE),
        ("set_carpet_mode", CarpetMode.AVOID),
    ]


async def test_two_stage_cleanup_failure_does_not_mask_the_original_error():
    clock = FakeClock()
    gw = _ErrorsMidCleanRobot(clock=clock)

    async def broken_dock():
        raise ConnectionError("cloud down")

    gw.dock = broken_dock
    with pytest.raises(AutomationError, match="brush stuck"):
        await run_two_stage(
            gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
        )


async def test_two_stage_cancel_mid_vacuum_docks_and_restores_carpet():
    """A user cancel mid-run is a first-class outcome, not an error: the run stops at the
    next poll, sends the robot home, restores the safe carpet Avoid (the same recovery as
    any abort), and raises RunCancelled so the caller can report 'cancelled'. The mop stage
    never starts."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)  # machine mode: docked -> cleaning -> docked
    cancel = asyncio.Event()
    stages: list[str] = []

    def on_stage(stage: str) -> None:
        stages.append(stage)
        if stage == "vacuum_waiting":  # robot confirmed out vacuuming: cancel mid-vacuum
            cancel.set()

    with pytest.raises(RunCancelled):
        await run_two_stage(
            gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5,
            log=lambda m: None, on_stage=on_stage, cancel=cancel,
        )
    assert stages == ["vacuuming", "vacuum_waiting"]
    assert gw.commands == [
        ("set_carpet_mode", CarpetMode.RISE),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("start",),
        ("dock",),
        ("set_carpet_mode", CarpetMode.AVOID),
    ]


async def test_two_stage_happy_path_does_not_dock_or_reset():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    await run_two_stage(
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5, log=lambda m: None
    )
    assert ("dock",) not in gw.commands


RUN_EPOCH = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


def _wall_clock(clock: FakeClock):
    """A wall clock that tracks the fake monotonic clock, so started_at is deterministic."""
    return lambda: RUN_EPOCH + timedelta(seconds=clock.now)


async def test_two_stage_records_one_cleaning_run_per_pass():
    """Each pass writes its own Cleaning Run with its real mode — vacuum then mop — once
    the robot has genuinely departed for that pass. The recording lives in the procedure,
    so every caller (button or cron) gets the same trace."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    log = _log()
    await run_two_stage(
        gw, _config(), cleaning_log=log, source=CleanSource.CRON,
        clock=clock, sleep=clock.sleep, interval=5, segment_ids=[16, 17],
        log=lambda m: None, now=_wall_clock(clock),
    )
    assert await log.list_runs() == [
        CleaningRun(source=CleanSource.CRON, mode=CleaningMode.VACUUM,
                    segment_ids=(16, 17), started_at=RUN_EPOCH + timedelta(seconds=10)),
        CleaningRun(source=CleanSource.CRON, mode=CleaningMode.MOP,
                    segment_ids=(16, 17), started_at=RUN_EPOCH + timedelta(seconds=45)),
    ]


class _MopRefusedRobot(FakeRobot):
    """Machine-mode robot whose firmware refuses to enter mop mode."""

    async def set_cleaning_mode(self, mode: CleaningMode) -> None:
        if mode is CleaningMode.MOP:
            raise ConnectionError("mop mode refused")
        await super().set_cleaning_mode(mode)


async def test_failed_mop_pass_still_records_the_vacuum_pass():
    """A mop pass that never starts must leave the vacuum's Cleaning Run in place: one
    honest record of what actually happened, not zero and not a synthetic pair."""
    clock = FakeClock()
    gw = _MopRefusedRobot(clock=clock)
    log = _log()
    with pytest.raises(ConnectionError, match="mop mode refused"):
        await run_two_stage(
            gw, _config(), cleaning_log=log, source=CleanSource.APP,
            clock=clock, sleep=clock.sleep, interval=5,
            log=lambda m: None, now=_wall_clock(clock),
        )
    assert await log.list_runs() == [
        CleaningRun(source=CleanSource.APP, mode=CleaningMode.VACUUM,
                    segment_ids=None, started_at=RUN_EPOCH + timedelta(seconds=10)),
    ]


# --- The N-Step Sequence runner ---------------------------------------------------------
# The Two-Stage Clean above is one preset of this runner; these tests pin the general
# procedure over the machine-mode fake (depart 3s after the trigger, clean for 30s), so
# with a 5s poll each Step departs 10s after its trigger and is home 25s after that.

BATHROOMS_RISE = Step(mode=CleaningMode.VACUUM, carpet=CarpetMode.RISE, segment_ids=(3, 5))
HOUSE_AVOID = Step(mode=CleaningMode.VAC_AND_MOP, carpet=CarpetMode.AVOID, segment_ids=None)
KITCHEN_MOP = Step(mode=CleaningMode.MOP, carpet=CarpetMode.AVOID, segment_ids=(17,))

END_RESET = [("set_carpet_mode", CarpetMode.AVOID)]
ABORT_RECOVERY = [("dock",), ("set_carpet_mode", CarpetMode.AVOID)]


def _step_commands(step: Step) -> list[tuple]:
    """What one Step sends: carpet, then mode, then the trigger for its scope."""
    depart = ("start",) if step.segment_ids is None else ("clean_rooms", step.segment_ids)
    return [("set_carpet_mode", step.carpet), ("set_cleaning_mode", step.mode), depart]


def _phases(index: int) -> list[tuple[int, Phase]]:
    return [(index, phase) for phase in Phase]


def _run_of(step: Step, *, departed_at: float, source=CleanSource.APP) -> CleaningRun:
    return CleaningRun(
        source=source, mode=step.mode, segment_ids=step.segment_ids,
        started_at=RUN_EPOCH + timedelta(seconds=departed_at),
    )


async def _run_steps(gw, clock, steps, **over):
    kwargs = dict(
        cleaning_log=_log(), source=CleanSource.APP, clock=clock, sleep=clock.sleep,
        interval=5, log=lambda m: None, now=_wall_clock(clock),
    )
    kwargs.update(over)
    await run_sequence(gw, steps, _config(), **kwargs)


async def test_sequence_runs_each_step_in_order_with_its_own_carpet_mode_and_segments():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    log = _log()
    progress: list[tuple[int, Phase]] = []
    await _run_steps(
        gw, clock, (BATHROOMS_RISE, HOUSE_AVOID, KITCHEN_MOP),
        cleaning_log=log, on_progress=lambda i, phase: progress.append((i, phase)),
    )
    assert gw.commands == (
        _step_commands(BATHROOMS_RISE)
        + _step_commands(HOUSE_AVOID)
        + _step_commands(KITCHEN_MOP)
    )
    assert progress == _phases(0) + _phases(1) + _phases(2)
    assert await log.list_runs() == [
        _run_of(BATHROOMS_RISE, departed_at=10),
        _run_of(HOUSE_AVOID, departed_at=45),
        _run_of(KITCHEN_MOP, departed_at=80),
    ]


async def test_a_successful_sequence_ending_at_rise_still_leaves_carpet_at_avoid():
    """Plain Cleans never touch carpet mode, so a Sequence left at Rise would send the
    next plain Clean onto the big carpets."""
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    await _run_steps(gw, clock, (BATHROOMS_RISE,))
    assert gw.commands == _step_commands(BATHROOMS_RISE) + END_RESET
    assert await gw.get_carpet_mode() is CarpetMode.AVOID


async def test_a_failure_in_a_later_step_recovers_and_records_only_the_steps_that_departed():
    clock = FakeClock()
    gw = _MopRefusedRobot(clock=clock)
    log = _log()
    progress: list[tuple[int, Phase]] = []
    mop_house = Step(mode=CleaningMode.MOP, carpet=CarpetMode.AVOID, segment_ids=None)
    with pytest.raises(ConnectionError, match="mop mode refused"):
        await _run_steps(
            gw, clock, (BATHROOMS_RISE, mop_house, HOUSE_AVOID),
            cleaning_log=log, on_progress=lambda i, phase: progress.append((i, phase)),
        )
    assert gw.commands == (
        _step_commands(BATHROOMS_RISE)
        + [("set_carpet_mode", CarpetMode.AVOID)]  # Step 2's carpet, before its mode fails
        + ABORT_RECOVERY
    )
    assert progress == _phases(0) + [(1, Phase.SETTING_UP)]
    assert await log.list_runs() == [_run_of(BATHROOMS_RISE, departed_at=10)]


async def test_cancel_mid_step_recovers_and_keeps_the_runs_of_every_departed_step():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    log = _log()
    cancel = asyncio.Event()

    def on_progress(index: int, phase: Phase) -> None:
        if (index, phase) == (1, Phase.CLEANING):
            cancel.set()

    with pytest.raises(RunCancelled):
        await _run_steps(
            gw, clock, (BATHROOMS_RISE, HOUSE_AVOID, KITCHEN_MOP),
            cleaning_log=log, on_progress=on_progress, cancel=cancel,
        )
    assert gw.commands == (
        _step_commands(BATHROOMS_RISE) + _step_commands(HOUSE_AVOID) + ABORT_RECOVERY
    )
    assert await log.list_runs() == [
        _run_of(BATHROOMS_RISE, departed_at=10),
        _run_of(HOUSE_AVOID, departed_at=45),
    ]


async def test_on_first_departure_fires_exactly_once_when_the_first_step_leaves_the_dock():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    fired_at: list[float] = []

    async def on_first_departure() -> None:
        fired_at.append(clock.now)

    await _run_steps(
        gw, clock, (BATHROOMS_RISE, HOUSE_AVOID, KITCHEN_MOP),
        on_first_departure=on_first_departure,
    )
    assert fired_at == [10.0]


async def test_on_first_departure_never_fires_when_the_first_step_fails_before_leaving():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    gw.set_timeline([(0, replace(DOCKED_IDLE, cleaning_mode="vacuum"))])  # every trigger is dropped
    fired_at: list[float] = []

    async def on_first_departure() -> None:
        fired_at.append(clock.now)

    with pytest.raises(AutomationError, match="did not take"):
        await _run_steps(gw, clock, (BATHROOMS_RISE,), on_first_departure=on_first_departure)
    assert fired_at == []


async def test_two_stage_is_the_vacuum_rise_then_mop_avoid_preset_with_no_extra_writes():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    await run_two_stage(
        gw, _config(), cleaning_log=_log(), source=CleanSource.APP,
        clock=clock, sleep=clock.sleep, interval=5, segment_ids=[16, 17], log=lambda m: None,
    )
    vacuum = Step(mode=CleaningMode.VACUUM, carpet=CarpetMode.RISE, segment_ids=(16, 17))
    mop = Step(mode=CleaningMode.MOP, carpet=CarpetMode.AVOID, segment_ids=(16, 17))
    # Its last Step already set and read back Avoid, so nothing follows the mop trigger.
    assert gw.commands == _step_commands(vacuum) + _step_commands(mop)


class _AvoidIgnoredRobot(FakeRobot):
    """Machine-mode robot that accepts a carpet Avoid write but never applies it."""

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        if mode is CarpetMode.AVOID:
            self.commands.append(("set_carpet_mode", mode))
            return
        await super().set_carpet_mode(mode)


async def test_a_final_avoid_that_does_not_take_after_a_rise_step_fails_with_recovery():
    clock = FakeClock()
    gw = _AvoidIgnoredRobot(clock=clock)
    gw._carpet = CarpetMode.RISE  # so the ignored Avoid reads back as Rise
    log = _log()
    with pytest.raises(AutomationError, match="carpet mode did not take"):
        await _run_steps(gw, clock, (BATHROOMS_RISE,), cleaning_log=log)
    assert gw.commands == _step_commands(BATHROOMS_RISE) + END_RESET + ABORT_RECOVERY
    assert await log.list_runs() == [_run_of(BATHROOMS_RISE, departed_at=10)]


async def test_a_raising_on_first_departure_never_aborts_the_clean():
    clock = FakeClock()
    gw = FakeRobot(clock=clock)
    log = _log()
    calls: list[float] = []

    async def on_first_departure() -> None:
        calls.append(clock.now)
        raise RuntimeError("occurrence store unavailable")

    await _run_steps(
        gw, clock, (BATHROOMS_RISE, HOUSE_AVOID),
        cleaning_log=log, on_first_departure=on_first_departure,
    )
    assert calls == [10.0]
    assert gw.commands == _step_commands(BATHROOMS_RISE) + _step_commands(HOUSE_AVOID)
    assert await log.list_runs() == [
        _run_of(BATHROOMS_RISE, departed_at=10),
        _run_of(HOUSE_AVOID, departed_at=45),
    ]


# --- RunSlot ------------------------------------------------------------------------


def test_the_run_slot_admits_one_run_at_a_time_with_a_fresh_cancel_per_run():
    slot = RunSlot()
    assert slot.running is False
    assert slot.acquire() is True
    first_cancel = slot.cancel
    assert slot.running is True
    assert slot.acquire() is False  # a second run is refused while the first holds it
    first_cancel.set()
    slot.release()
    assert slot.running is False
    assert slot.acquire() is True
    assert slot.cancel is not first_cancel
    assert slot.cancel.is_set() is False
