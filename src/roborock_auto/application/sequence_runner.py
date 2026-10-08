"""The Sequence runner: run an ordered list of Steps, expressed purely over the ports.

Each Step sets and reads back its carpet mode, sets its cleaning mode, triggers the clean,
and waits for the robot to leave and come home before the next Step starts. The completion
check is depart-then-return: confirm the job actually started, then wait for the robot to
be home and idle. Charging alone is not "done" because the robot recharges mid-job and
resumes (see domain/policies.py), and that recharge must not start the next Step early.

clock and sleep are injected so the timeouts are deterministic under test with no real waits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from roborock_auto.application.retry import AutomationError, DepartRejected, Sleep, with_retry
from roborock_auto.application.start_clean import utc_now, verify_cleaning_mode
from roborock_auto.config import Config
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningRun,
    CleanSource,
    DeviceStatus,
    Step,
)
from roborock_auto.domain.policies import (
    has_departed,
    home_and_idle,
    job_stalled,
    ready_to_start,
    recharging_pending,
)
from roborock_auto.ports.cleaning_log import CleaningLogPort
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


class SequencePorts(RobotStatusPort, CleaningControlPort, CleaningSettingsPort, Protocol):
    """Everything this use case needs from the robot — and nothing more."""


class Phase(Enum):
    """Where one Step is, in the order the runner reports it."""

    SETTING_UP = "setting_up"   # carpet mode set and read back, cleaning mode set
    DEPARTING = "departing"     # trigger sent, waiting for a confirmed Departure
    CLEANING = "cleaning"       # departed, waiting for the robot to be home and idle
    DONE = "done"


class RunCancelled(Exception):
    """The user cancelled the in-flight run. Raised out of run_sequence after the same
    recovery as any abort (dock + carpet Avoid), typed so the caller can report 'cancelled'
    rather than an error."""


def _cancellable_sleep(sleep: Sleep, cancel: asyncio.Event) -> Sleep:
    """Wrap a sleep so it wakes the moment cancel is set (not at the end of the poll
    interval) and raises RunCancelled. Every wait in the run funnels through sleep, so this
    one seam makes the whole orchestration promptly cancellable."""

    async def _sleep(seconds: float) -> None:
        if cancel.is_set():
            raise RunCancelled("run cancelled")
        sleeper = asyncio.ensure_future(sleep(seconds))
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            waiter.cancel()
        if cancel.is_set():
            raise RunCancelled("run cancelled")

    return _sleep


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
    otherwise a later Step 'departs' on leftover bin-emptying. Two failure paths: the robot
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


async def run_sequence(
    gw: SequencePorts,
    steps: tuple[Step, ...],
    config: Config,
    *,
    cleaning_log: CleaningLogPort,
    source: CleanSource,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    now: Callable[[], datetime] = utc_now,
    interval: float | None = None,
    log: Callable[[str], None] = print,
    on_progress: Callable[[int, Phase], None] = lambda _index, _phase: None,
    on_first_departure: Callable[[], Awaitable[None]] | None = None,
    cancel: asyncio.Event | None = None,
) -> None:
    poll = interval if interval is not None else config.poll_interval_sec
    if cancel is not None:
        # RunCancelled raised from any sleep unwinds through the abort path below, so a
        # cancel gets the same recovery (dock + carpet Avoid) as any other mid-run failure.
        sleep = _cancellable_sleep(sleep, cancel)
    # The idle grace must span several polls, or a taken trigger reads as dropped after a
    # sample or two and gets re-issued into an already-departing robot.
    if poll <= 0 or poll * 3 > DEPART_IDLE_GRACE:
        raise AutomationError(
            f"poll interval {poll}s is too coarse for the {DEPART_IDLE_GRACE:.0f}s departure "
            f"grace; set poll_interval_sec below {DEPART_IDLE_GRACE / 3:.0f}s"
        )

    start = await gw.get_status()
    if not ready_to_start(start):
        raise AutomationError(
            f"robot not docked/idle at start (state={start.state_name}); aborting"
        )

    # Any failure past this point can leave the robot mid-floor with wet pads down and carpet
    # mode latched at Rise; recover best-effort before re-raising. Only past this point — an
    # abort before we start (e.g. an official-app job running) must not dock someone's clean.
    try:
        for index, step in enumerate(steps):
            label = f"step {index + 1}/{len(steps)}"
            scope = "whole-house" if step.segment_ids is None else "selected rooms"
            log(
                f"{label}: carpet -> {step.carpet.value}, clean mode -> {step.mode.value}, "
                f"starting {scope} clean"
            )
            on_progress(index, Phase.SETTING_UP)
            # Read-back before departing: if Avoid did not stick, wet pads would cross carpet.
            await _set_carpet_verified(gw, step.carpet, sleep=sleep)
            await with_retry(lambda mode=step.mode: gw.set_cleaning_mode(mode), sleep=sleep)
            await verify_cleaning_mode(gw, step.mode)
            on_progress(index, Phase.DEPARTING)
            await _depart_and_confirm(
                gw, _depart_for(gw, step), clock=clock, sleep=sleep, interval=poll, log=log
            )
            # Before the Cleaning Run write: if that write fails, the caller must already
            # know the robot left, or it would treat the run as never started and retry it.
            if index == 0 and on_first_departure is not None:
                try:
                    await on_first_departure()
                except Exception as exc:  # noqa: BLE001 - bookkeeping must not abort a real clean
                    log(f"{label}: first-departure callback failed: {exc}")
            # One Cleaning Run per Step, written here rather than by the caller, so a later
            # Step's failure still leaves an honest record of the Steps that happened.
            await cleaning_log.record_run(
                CleaningRun(
                    source=source, mode=step.mode, segment_ids=step.segment_ids,
                    started_at=now(),
                )
            )
            log(f"{label}: robot left the dock, waiting for it to finish")
            on_progress(index, Phase.CLEANING)
            await wait_for_return(gw, clock=clock, sleep=sleep, interval=poll)
            log(f"{label}: complete")
            on_progress(index, Phase.DONE)
        # Plain Cleans never touch carpet mode, so a Sequence that ended at Rise would send
        # the next plain Clean (from any app or rule) onto the carpets it must avoid. A last
        # Step at Avoid already set and read it back, so a second write only adds a way to fail.
        if steps[-1].carpet is not CarpetMode.AVOID:
            await _set_carpet_verified(
                gw, CarpetMode.AVOID, sleep=sleep,
                consequence="the clean finished but carpet may still be at Rise",
            )
    # CancelledError is not an Exception; it is how a server shutdown ends the run, and
    # that leaves the robot just as stranded as any other abort.
    except (Exception, asyncio.CancelledError):
        await _recover_uninterrupted(gw, log=log)
        raise


def _depart_for(gw: SequencePorts, step: Step) -> Callable[[], Awaitable[None]]:
    if step.segment_ids is None:
        return gw.start
    segments = list(step.segment_ids)
    return lambda: gw.clean_rooms(segments, repeat=step.repeat)


async def _recover_uninterrupted(gw: SequencePorts, *, log: Callable[[str], None]) -> None:
    """Run abort recovery to the end even if the run is cancelled again meanwhile, as a
    shutdown landing mid-recovery would otherwise skip the rest of it; then let that
    cancellation through."""
    recovery = asyncio.create_task(_recover_after_abort(gw, log=log))
    cancelled = False
    while not recovery.done():
        try:
            await asyncio.shield(recovery)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


async def _recover_after_abort(gw: SequencePorts, *, log: Callable[[str], None]) -> None:
    """Best-effort abort recovery: latch carpet mode to the safe Avoid (never wets carpet),
    then send the robot home. Avoid goes first because it also covers the drive home, and a
    shutdown's wait may run out on a slow dock reply. Each step swallows its own failure —
    recovery must never mask the error that aborted the run."""
    try:
        await gw.set_carpet_mode(CarpetMode.AVOID)
    except Exception as exc:  # noqa: BLE001 - recovery must never mask the original error
        log(f"abort recovery: carpet reset failed: {exc}")
    try:
        await gw.dock()
    except Exception as exc:  # noqa: BLE001 - recovery must never mask the original error
        log(f"abort recovery: dock command failed: {exc}")


async def _depart_and_confirm(
    gw: SequencePorts, depart: Callable[[], Any], *,
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


async def _set_carpet_verified(
    gw: CleaningSettingsPort, mode: CarpetMode, *, sleep: Sleep,
    consequence: str = "aborting before starting the clean",
) -> None:
    await with_retry(lambda: gw.set_carpet_mode(mode), sleep=sleep)
    actual = await gw.get_carpet_mode()
    if actual != mode:
        raise AutomationError(
            f"carpet mode did not take: asked {mode.value}, device reports {actual.value}; "
            f"{consequence}"
        )
