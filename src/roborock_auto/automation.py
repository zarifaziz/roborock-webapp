"""Carpet-aware two-stage clean, expressed purely over the Gateway.

Vacuum with carpet Rise, wait for the robot to leave and come home, then mop with carpet
Avoid. The completion check is depart-then-return: confirm the job actually started, then
wait for the robot to be docked AND not in_cleaning. Charging alone is not "done" because the
robot recharges mid-job and resumes, and that recharge must not trigger the mop stage early.

clock and sleep are injected so the timeouts are deterministic under test with no real waits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from roborock_auto.config import Config
from roborock_auto.gateway.base import Gateway
from roborock_auto.models import CarpetMode, CleaningMode

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

DEPART_TIMEOUT = 180.0     # 3 min: if the job never starts, the scene failed to trigger
RETURN_TIMEOUT = 7200.0    # 2 h: a stuck robot fails loudly instead of hanging forever


class AutomationError(Exception):
    pass


async def with_retry(
    fn: Callable[[], Awaitable[None]],
    *,
    sleep: Sleep,
    attempts: int = 3,
    base_delay: float = 2.0,
) -> None:
    for i in range(attempts):
        try:
            await fn()
            return
        except AutomationError:
            raise
        except Exception:
            if i == attempts - 1:
                raise
            await sleep(base_delay * (2**i))


async def wait_for_departure(
    gw: Gateway, *, clock: Clock, sleep: Sleep, interval: float, timeout: float = DEPART_TIMEOUT
) -> None:
    """Return once a job is confirmed active. Raise if it never starts."""
    deadline = clock() + timeout
    while True:
        status = await gw.get_status()
        if status.error:
            raise AutomationError(f"device error while waiting to depart: {status.error}")
        if status.in_cleaning:
            return
        if clock() >= deadline:
            raise AutomationError("job never started (scene did not trigger)")
        await sleep(interval)


async def wait_for_return(
    gw: Gateway, *, clock: Clock, sleep: Sleep, interval: float, timeout: float = RETURN_TIMEOUT
) -> None:
    """Return once the robot is home and idle. A mid-job recharge keeps in_cleaning true."""
    deadline = clock() + timeout
    while True:
        status = await gw.get_status()
        if status.error:
            raise AutomationError(f"device error during clean: {status.error}")
        if status.docked and not status.in_cleaning:
            return
        if clock() >= deadline:
            raise AutomationError("timed out waiting for the robot to return")
        await sleep(interval)


async def run_two_stage(
    gw: Gateway,
    config: Config,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    interval: float | None = None,
    log: Callable[[str], None] = print,
) -> None:
    poll = interval if interval is not None else config.poll_interval_sec

    start = await gw.get_status()
    if start.in_cleaning or not start.docked:
        raise AutomationError(
            f"robot not docked/idle at start (state={start.state_name}); aborting"
        )

    log("vacuum stage: carpet -> Rise, clean mode -> vacuum, starting whole-house clean")
    await with_retry(lambda: gw.set_carpet_mode(CarpetMode.RISE), sleep=sleep)
    await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.VACUUM), sleep=sleep)
    await with_retry(gw.start, sleep=sleep)
    await wait_for_departure(gw, clock=clock, sleep=sleep, interval=poll)
    log("vacuum stage: robot left the dock, waiting for it to finish")
    await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
    log("vacuum stage: complete")

    log("mop stage: carpet -> Avoid, clean mode -> mop, starting whole-house clean")
    await with_retry(lambda: gw.set_carpet_mode(CarpetMode.AVOID), sleep=sleep)
    await with_retry(lambda: gw.set_cleaning_mode(CleaningMode.MOP), sleep=sleep)
    await with_retry(gw.start, sleep=sleep)
    await wait_for_departure(gw, clock=clock, sleep=sleep, interval=poll)
    log("mop stage: robot left the dock, waiting for it to finish")
    await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
    log("mop stage: complete")
