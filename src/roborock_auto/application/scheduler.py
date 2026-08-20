"""The scheduler: the tick that fires due Occurrences, and the loop that runs it.

A tick is an orchestration over five named collaborators — the Materialiser
(materialise), the selector of claimable Occurrences (claimable_occurrences), the
readiness gate over robot status (readiness_block), the firer (fire), and the recorder
of outcomes (OutcomeRecorder). Firing is a claim, not a decision (docs/adr/0002), and it
goes through the shared clean use case so a scheduled clean cannot drift from the
button. The loop survives any tick exception and backs off exponentially, capped, so an
offline robot does not log an error twice a minute indefinitely.

Wall clock and sleep are injected so the tests run with zero real waits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from typing import Protocol

from roborock_auto.application.materialise import materialise
from roborock_auto.application.start_clean import CleanPorts, start_clean, utc_now
from roborock_auto.domain.models import CleanAction, CleanSource, DeviceStatus, Occurrence
from roborock_auto.domain.policies import (
    OCCURRENCE_MAX_ATTEMPTS,
    claimable_occurrences,
    occurrence_expired,
    ready_to_start,
)
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.occurrences import OccurrencePort
from roborock_auto.ports.robot import RobotStatusPort
from roborock_auto.ports.schedule_rules import ScheduleRulePort

_LOGGER = logging.getLogger("roborock_auto.scheduler")

TICK_INTERVAL_SEC = 30.0
MAX_BACKOFF_SEC = 300.0


class SchedulerPorts(RobotStatusPort, CleanPorts, Protocol):
    """Everything the scheduler needs from the robot — and nothing more."""


def readiness_block(status: DeviceStatus, *, two_stage_running: bool) -> str | None:
    """The readiness gate: the reason firing must wait, or None to proceed. The verdict
    is the ready_to_start policy unchanged; the branches only name which leg failed,
    plus the one condition status cannot see — a Two-Stage Clean in flight in this
    process."""
    if two_stage_running:
        return "a two-stage clean is in flight"
    if ready_to_start(status):
        return None
    if status.error is not None:
        return f"the robot reports an error: {status.error}"
    if status.in_cleaning:
        return "the robot is mid-job"
    return "the robot is not docked"


async def fire(
    gw: CleanPorts,
    *,
    cleaning_log: CleaningLogPort,
    action: CleanAction,
    now: Callable[[], datetime],
) -> None:
    """The firer: the shared clean use case under the SCHEDULER source. The scheduler
    re-implements no device logic."""
    await start_clean(
        gw,
        cleaning_log=cleaning_log,
        source=CleanSource.SCHEDULER,
        mode=action.mode,
        segment_ids=None if action.segment_ids is None else list(action.segment_ids),
        now=now,
    )


class OutcomeRecorder:
    """The recorder: writes each Occurrence state transition to the store and emits one
    structured log line per transition."""

    def __init__(self, occurrences: OccurrencePort) -> None:
        self._occurrences = occurrences

    async def expired(self, occurrence: Occurrence) -> None:
        await self._occurrences.mark_expired(occurrence.id)
        _LOGGER.info(
            "occurrence=%d rule=%d state=expired scheduled_for=%s deadline=%s",
            occurrence.id, occurrence.rule_id, occurrence.scheduled_for, occurrence.deadline,
        )

    async def blocked(self, occurrence: Occurrence, reason: str) -> None:
        await self._occurrences.mark_blocked(occurrence.id, reason)
        _LOGGER.info(
            "occurrence=%d rule=%d state=pending blocked_reason=%r scheduled_for=%s",
            occurrence.id, occurrence.rule_id, reason, occurrence.scheduled_for,
        )

    async def fired(self, occurrence: Occurrence, at: datetime) -> None:
        await self._occurrences.mark_fired(occurrence.id, at)
        _LOGGER.info(
            "occurrence=%d rule=%d state=fired fired_at=%s scheduled_for=%s",
            occurrence.id, occurrence.rule_id, at, occurrence.scheduled_for,
        )

    async def failed_attempt(self, occurrence: Occurrence, reason: str, at: datetime) -> None:
        attempts = occurrence.attempts + 1
        exhausted = attempts >= OCCURRENCE_MAX_ATTEMPTS
        await self._occurrences.record_failed_attempt(
            occurrence.id, at=at, reason=reason, exhausted=exhausted
        )
        _LOGGER.warning(
            "occurrence=%d rule=%d state=%s attempts=%d reason=%r",
            occurrence.id, occurrence.rule_id,
            "failed" if exhausted else "pending", attempts, reason,
        )


class Scheduler:
    def __init__(
        self,
        robot: SchedulerPorts,
        *,
        rules: ScheduleRulePort,
        occurrences: OccurrencePort,
        cleaning_log: CleaningLogPort,
        tz: tzinfo,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        two_stage_running: Callable[[], bool] = lambda: False,
    ) -> None:
        self._robot = robot
        self._rules = rules
        self._occurrences = occurrences
        self._cleaning_log = cleaning_log
        self._tz = tz
        self._now = now
        self._sleep = sleep
        self._two_stage_running = two_stage_running
        self._recorder = OutcomeRecorder(occurrences)
        self.last_tick_at: datetime | None = None
        self.consecutive_errors = 0

    def backoff_sec(self) -> float:
        return min(TICK_INTERVAL_SEC * 2**self.consecutive_errors, MAX_BACKOFF_SEC)

    async def run(self) -> None:
        _LOGGER.info("scheduler started timezone=%s tick_interval_sec=%s", self._tz,
                     TICK_INTERVAL_SEC)
        while True:
            await self._sleep(self.backoff_sec())
            await self.tick_safely()

    async def tick_safely(self) -> None:
        try:
            await self.tick()
        except Exception:  # the loop must survive any tick failure
            self.consecutive_errors += 1
            _LOGGER.exception(
                "tick failed consecutive_errors=%d backoff_sec=%s",
                self.consecutive_errors, self.backoff_sec(),
            )
        else:
            self.consecutive_errors = 0
        self.last_tick_at = self._now()

    async def tick(self) -> None:
        now = self._now()
        await materialise(self._rules, self._occurrences, now=now, tz=self._tz)
        occurrences = await self._occurrences.list_occurrences()
        for occurrence in occurrences:
            if occurrence_expired(occurrence, now):
                await self._recorder.expired(occurrence)
        due = claimable_occurrences(occurrences, now)
        if not due:
            return
        actions = {rule.id: rule.action for rule in await self._rules.list_rules()}
        for occurrence in due:
            # Gated per Occurrence, not per tick: the first firing makes the robot busy,
            # so the second of two simultaneous ones waits and fires on a later tick.
            reason = readiness_block(
                await self._robot.get_status(), two_stage_running=self._two_stage_running()
            )
            if reason is not None:
                await self._recorder.blocked(occurrence, reason)
                continue
            if not await self._occurrences.claim(occurrence.id):
                continue
            try:
                await fire(
                    self._robot,
                    cleaning_log=self._cleaning_log,
                    action=actions[occurrence.rule_id],
                    now=self._now,
                )
            except Exception as exc:  # noqa: BLE001 - a refusal is an outcome, not a crash
                await self._recorder.failed_attempt(occurrence, str(exc), self._now())
            else:
                await self._recorder.fired(occurrence, self._now())
