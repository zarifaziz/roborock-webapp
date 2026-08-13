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
from typing import Protocol

from roborock_auto.application.retry import AutomationError, Sleep, with_retry
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, CleaningMode
from roborock_auto.domain.policies import (
    home_and_idle,
    job_is_active,
    job_stalled,
    ready_to_start,
)
from roborock_auto.ports.robot import CleaningControlPort, CleaningSettingsPort, RobotStatusPort

Clock = Callable[[], float]

DEPART_TIMEOUT = 180.0     # 3 min: if the job never starts, the trigger failed
RETURN_TIMEOUT = 7200.0    # 2 h: a stuck robot fails loudly instead of hanging forever
STALL_TIMEOUT = 300.0      # 5 min paused/cancelled: fail fast instead of burning the 2 h


class TwoStagePorts(RobotStatusPort, CleaningControlPort, CleaningSettingsPort, Protocol):
    """Everything this use case needs from the robot — and nothing more."""


async def wait_for_departure(
    gw: RobotStatusPort, *, clock: Clock, sleep: Sleep, interval: float,
    timeout: float = DEPART_TIMEOUT,
) -> None:
    """Return once a job is confirmed active. Raise if it never starts."""
    deadline = clock() + timeout
    while True:
        status = await gw.get_status()
        if status.error:
            raise AutomationError(f"device error while waiting to depart: {status.error}")
        if job_is_active(status):
            return
        if clock() >= deadline:
            raise AutomationError("job never started (trigger did not take)")
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
    while True:
        status = await gw.get_status()
        if status.error:
            raise AutomationError(f"device error during clean: {status.error}")
        if home_and_idle(status):
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
    log: Callable[[str], None] = print,
    on_stage: Callable[[str], None] = lambda _stage: None,
) -> None:
    poll = interval if interval is not None else config.poll_interval_sec

    start = await gw.get_status()
    if not ready_to_start(start):
        raise AutomationError(
            f"robot not docked/idle at start (state={start.state_name}); aborting"
        )

    log("vacuum stage: carpet -> Rise, clean mode -> vacuum, starting whole-house clean")
    on_stage("vacuuming")
    await _set_carpet_verified(gw, CarpetMode.RISE, sleep=sleep)
    await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.VACUUM), sleep=sleep)
    await with_retry(gw.start, sleep=sleep)
    await wait_for_departure(gw, clock=clock, sleep=sleep, interval=poll)
    log("vacuum stage: robot left the dock, waiting for it to finish")
    on_stage("vacuum_waiting")
    await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
    log("vacuum stage: complete")

    log("mop stage: carpet -> Avoid, clean mode -> mop, starting whole-house clean")
    on_stage("mopping")
    # Read-back before mopping: if carpet Avoid did not stick, wet pads would cross carpet.
    await _set_carpet_verified(gw, CarpetMode.AVOID, sleep=sleep)
    await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.MOP), sleep=sleep)
    await with_retry(gw.start, sleep=sleep)
    await wait_for_departure(gw, clock=clock, sleep=sleep, interval=poll)
    log("mop stage: robot left the dock, waiting for it to finish")
    on_stage("mop_waiting")
    await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
    log("mop stage: complete")
    on_stage("done")


async def _set_carpet_verified(gw: CleaningSettingsPort, mode: CarpetMode, *, sleep: Sleep) -> None:
    await with_retry(lambda: gw.set_carpet_mode(mode), sleep=sleep)
    actual = await gw.get_carpet_mode()
    if actual != mode:
        raise AutomationError(
            f"carpet mode did not take: asked {mode.value}, device reports {actual.value}; "
            f"aborting before starting the clean"
        )
