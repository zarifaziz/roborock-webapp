"""Carpet-aware two-stage clean, expressed purely over the ports.

Vacuum with carpet Rise, wait for the robot to leave and come home, then mop with carpet
Avoid. The completion check is depart-then-return: confirm the job actually started, then
wait for the robot to be home and idle. Charging alone is not "done" because the robot
recharges mid-job and resumes (see domain/policies.py), and that recharge must not trigger
the mop stage early.

clock and sleep are injected so the timeouts are deterministic under test with no real waits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

from roborock_auto.application.retry import AutomationError, DepartRejected, Sleep, with_retry
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, CleaningMode, DeviceStatus
from roborock_auto.domain.policies import (
    has_departed,
    home_and_idle,
    job_stalled,
    ready_to_start,
    recharging_pending,
)
from roborock_auto.ports.robot import CleaningControlPort, CleaningSettingsPort, RobotStatusPort

Clock = Callable[[], float]

DEPART_TIMEOUT = 1800.0    # 30 min backstop: pre-mop dock servicing (mop wash) can be minutes
DEPART_IDLE_GRACE = 60.0   # settled idle on the dock this long = the trigger did not take
RETURN_TIMEOUT = 7200.0    # 2 h: a stuck robot fails loudly instead of hanging forever
STALL_TIMEOUT = 300.0      # 5 min paused/cancelled: fail fast instead of burning the 2 h
# A positive signal (departed, done, error) must hold across this many consecutive polls
# before we act on it, so one stale cached read or a one-poll firmware flag flicker can't
# truncate a stage or abort a healthy run.
CONFIRM_SAMPLES = 2
# Consecutive get_status failures tolerated as skipped polls before declaring contact lost.
# A cloud/MQTT blip must not abort an hours-long unattended run; a real outage still fails
# within budget+1 polls instead of hanging.
STATUS_FAULT_BUDGET = 5


class TwoStagePorts(RobotStatusPort, CleaningControlPort, CleaningSettingsPort, Protocol):
    """Everything this use case needs from the robot — and nothing more."""


async def _next_status(gw: RobotStatusPort, *, sleep: Sleep, interval: float) -> DeviceStatus:
    """One resilient status poll. A raised get_status (transport blip) is a skipped poll —
    sleep and retry — up to STATUS_FAULT_BUDGET consecutive failures; any success resets the
    streak because each poll starts a fresh budget."""
    last: Exception | None = None
    for _ in range(STATUS_FAULT_BUDGET + 1):
        try:
            return await gw.get_status()
        except Exception as exc:  # noqa: BLE001 - any transport blip is a skipped poll
            last = exc
            await sleep(interval)
    raise AutomationError(f"lost contact with the robot: {last}") from last


async def wait_for_departure(
    gw: RobotStatusPort, *, clock: Clock, sleep: Sleep, interval: float,
    timeout: float = DEPART_TIMEOUT, idle_grace: float = DEPART_IDLE_GRACE,
) -> None:
    """Return once the robot has actually left the dock to clean. Raise if it never does.

    Departure means a genuine cleaning state (confirmed across CONFIRM_SAMPLES polls, so a
    stale read can't fool it), not dock-servicing that lingers from the previous stage —
    otherwise the mop stage 'departs' on leftover bin-emptying. Two failure paths: the robot
    settling idle on the dock for idle_grace raises DepartRejected (the trigger didn't take;
    the caller re-issues), and an absolute `timeout` backstop bounds total pre-departure time
    — generous because the pre-mop wash can run minutes. Charge-before-depart (docked with
    the job still active) restarts the backstop: it is progress, not a hang.
    """
    deadline = clock() + timeout
    idle_since: float | None = None
    error_seen = departed_seen = 0
    while True:
        status = await _next_status(gw, sleep=sleep, interval=interval)
        error_seen = error_seen + 1 if status.error else 0
        if error_seen >= CONFIRM_SAMPLES:
            raise AutomationError(f"device error while waiting to depart: {status.error}")
        departed_seen = departed_seen + 1 if has_departed(status) else 0
        if departed_seen >= CONFIRM_SAMPLES:
            return
        if recharging_pending(status):
            # Charge-before-depart on a deep discharge: the job is pending, so the backstop
            # restarts from here — a >timeout charge must not abort a job that will run.
            deadline = clock() + timeout
        if home_and_idle(status):
            if idle_since is None:
                idle_since = clock()
            elif clock() - idle_since >= idle_grace:
                raise DepartRejected("job never started (trigger did not take)")
        else:
            idle_since = None
        if clock() >= deadline:
            raise AutomationError("job never started (departure timed out)")
        await sleep(interval)


async def wait_for_return(
    gw: RobotStatusPort, *, clock: Clock, sleep: Sleep, interval: float,
    timeout: float = RETURN_TIMEOUT, stall_timeout: float = STALL_TIMEOUT,
) -> None:
    """Return once the robot is home and idle. A mid-job recharge keeps the job active.

    A job that sits paused (or ends off-dock after a user cancel) for stall_timeout is
    aborted early: nobody is coming to resume an unattended run, and failing in minutes
    beats burning the full return timeout.
    """
    deadline = clock() + timeout
    stalled_since: float | None = None
    error_seen = done_seen = 0
    while True:
        status = await _next_status(gw, sleep=sleep, interval=interval)
        error_seen = error_seen + 1 if status.error else 0
        if error_seen >= CONFIRM_SAMPLES:
            raise AutomationError(f"device error during clean: {status.error}")
        done_seen = done_seen + 1 if home_and_idle(status) else 0
        if done_seen >= CONFIRM_SAMPLES:
            return
        if job_stalled(status):
            if stalled_since is None:
                stalled_since = clock()
            elif clock() - stalled_since >= stall_timeout:
                raise AutomationError(
                    f"job stalled (state={status.state_name}) for {int(stall_timeout)}s; aborting"
                )
        else:
            stalled_since = None
        if clock() >= deadline:
            raise AutomationError("timed out waiting for the robot to return")
        await sleep(interval)


async def run_two_stage(
    gw: TwoStagePorts,
    config: Config,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    interval: float | None = None,
    segment_ids: list[int] | None = None,
    log: Callable[[str], None] = print,
    on_stage: Callable[[str], None] = lambda _stage: None,
) -> None:
    poll = interval if interval is not None else config.poll_interval_sec
    # The idle grace must span several polls, or a taken trigger reads as dropped after a
    # sample or two and gets re-issued into an already-departing robot.
    if poll <= 0 or poll * 3 > DEPART_IDLE_GRACE:
        raise AutomationError(
            f"poll interval {poll}s is too coarse for the {DEPART_IDLE_GRACE:.0f}s departure "
            f"grace; set poll_interval_sec below {DEPART_IDLE_GRACE / 3:.0f}s"
        )
    # Whole-house departs with start(); a room-scoped run departs with clean_rooms(segments).
    depart = (lambda: gw.clean_rooms(segment_ids)) if segment_ids else gw.start
    scope = "selected rooms" if segment_ids else "whole-house"

    start = await gw.get_status()
    if not ready_to_start(start):
        raise AutomationError(
            f"robot not docked/idle at start (state={start.state_name}); aborting"
        )

    # Any failure past this point can leave the robot mid-floor with wet pads down and carpet
    # mode latched at Rise; recover best-effort before re-raising. Only past this point — an
    # abort before we start (e.g. an official-app job running) must not dock someone's clean.
    try:
        log(f"vacuum stage: carpet -> Rise, clean mode -> vacuum, starting {scope} clean")
        on_stage("vacuuming")
        await _set_carpet_verified(gw, CarpetMode.RISE, sleep=sleep)
        await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.VACUUM), sleep=sleep)
        await _depart_and_confirm(gw, depart, clock=clock, sleep=sleep, interval=poll, log=log)
        log("vacuum stage: robot left the dock, waiting for it to finish")
        on_stage("vacuum_waiting")
        await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
        log("vacuum stage: complete")

        log(f"mop stage: carpet -> Avoid, clean mode -> mop, starting {scope} clean")
        on_stage("mopping")
        # Read-back before mopping: if carpet Avoid did not stick, wet pads would cross carpet.
        await _set_carpet_verified(gw, CarpetMode.AVOID, sleep=sleep)
        await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.MOP), sleep=sleep)
        await _depart_and_confirm(gw, depart, clock=clock, sleep=sleep, interval=poll, log=log)
        log("mop stage: robot left the dock, waiting for it to finish")
        on_stage("mop_waiting")
        await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
    except BaseException:
        await _recover_after_abort(gw, log=log)
        raise
    log("mop stage: complete")
    on_stage("done")


async def _recover_after_abort(gw: TwoStagePorts, *, log: Callable[[str], None]) -> None:
    """Best-effort abort recovery: send the robot home and latch carpet mode to the safe
    Avoid (never wets carpet). Each step swallows its own failure — recovery must never
    mask the error that aborted the run."""
    try:
        await gw.dock()
    except Exception as exc:  # noqa: BLE001 - recovery must never mask the original error
        log(f"abort recovery: dock command failed: {exc}")
    try:
        await gw.set_carpet_mode(CarpetMode.AVOID)
    except Exception as exc:  # noqa: BLE001 - recovery must never mask the original error
        log(f"abort recovery: carpet reset failed: {exc}")


async def _depart_and_confirm(
    gw: TwoStagePorts, depart: Callable[[], Any], *,
    clock: Clock, sleep: Sleep, interval: float, log: Callable[[str], None],
    attempts: int = 3,
) -> None:
    """Trigger the clean and confirm the robot actually left the dock, re-issuing the trigger
    if it was dropped. A clean sent while the robot is still servicing the dock is silently
    ignored by the firmware (the live mop that 'didn't happen'); wait_for_departure raises
    DepartRejected when the robot settles idle instead of leaving, and we send it again."""
    for attempt in range(attempts):
        await with_retry(depart, sleep=sleep)
        try:
            await wait_for_departure(gw, clock=clock, sleep=sleep, interval=interval)
            return
        except DepartRejected:
            if attempt == attempts - 1:
                raise
            # Guard the re-issue against a TOCTOU: the robot may have left in the gap between
            # the last poll and now. Only re-send if it is still not out cleaning.
            if has_departed(await gw.get_status()):
                return
            log(f"clean trigger was dropped (robot idle); re-issuing (attempt {attempt + 2})")


async def _set_carpet_verified(gw: CleaningSettingsPort, mode: CarpetMode, *, sleep: Sleep) -> None:
    await with_retry(lambda: gw.set_carpet_mode(mode), sleep=sleep)
    actual = await gw.get_carpet_mode()
    if actual != mode:
        raise AutomationError(
            f"carpet mode did not take: asked {mode.value}, device reports {actual.value}; "
            f"aborting before starting the clean"
        )
