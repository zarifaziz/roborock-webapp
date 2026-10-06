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
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.sequence_launch import SequenceLauncher
from roborock_auto.application.sequence_runner import RunCancelled
from roborock_auto.application.start_clean import CleanPorts, start_clean, utc_now
from roborock_auto.domain.models import (
    CleanAction,
    CleanSource,
    DeviceStatus,
    Occurrence,
    SequenceAction,
)
from roborock_auto.domain.policies import (
    OCCURRENCE_MAX_ATTEMPTS,
    claimable_occurrences,
    occurrence_expired,
    ready_to_start,
)
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.occurrences import OccurrencePort
from roborock_auto.ports.schedule_rules import ScheduleRulePort
from roborock_auto.ports.sequences import SequencePort

_LOGGER = logging.getLogger("roborock_auto.scheduler")

TICK_INTERVAL_SEC = 30.0
MAX_BACKOFF_SEC = 300.0


class SchedulerPorts(CleanPorts, Protocol):
    """Everything the scheduler needs from the robot — and nothing more."""


def readiness_block(status: DeviceStatus, *, run_in_flight: bool) -> str | None:
    """The readiness gate: the reason firing must wait, or None to proceed. The verdict
    is the ready_to_start policy unchanged; the branches only name which leg failed,
    plus the one condition status cannot see — a run holding the RunSlot in this
    process, which sits docked and idle between its Steps."""
    if run_in_flight:
        return "another run is in flight"
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

    async def cancelled(self, occurrence: Occurrence, at: datetime) -> None:
        # Terminal on purpose: the user stopped this run, so retrying would start the
        # very clean they just cancelled.
        await self._occurrences.record_failed_attempt(
            occurrence.id, at=at, reason="cancelled", exhausted=True
        )
        _LOGGER.info(
            "occurrence=%d rule=%d state=failed reason='cancelled'",
            occurrence.id, occurrence.rule_id,
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
        run_slot: RunSlot | None = None,
        sequences: SequencePort | None = None,
        launcher: SequenceLauncher | None = None,
    ) -> None:
        self._robot = robot
        self._rules = rules
        self._occurrences = occurrences
        self._cleaning_log = cleaning_log
        self._tz = tz
        self._now = now
        self._sleep = sleep
        self._run_slot = run_slot if run_slot is not None else RunSlot()
        self._sequences = sequences
        self._launcher = launcher
        self._recorder = OutcomeRecorder(occurrences)
        self.last_tick_at: datetime | None = None
        self.consecutive_errors = 0

    def backoff_sec(self) -> float:
        # The exponent is capped before the power: 2**consecutive_errors would
        # overflow float conversion after a long enough outage.
        return min(TICK_INTERVAL_SEC * 2 ** min(self.consecutive_errors, 4), MAX_BACKOFF_SEC)

    async def run(self) -> None:
        await self._fail_interrupted()
        _LOGGER.info("scheduler started timezone=%s tick_interval_sec=%s", self._tz,
                     TICK_INTERVAL_SEC)
        while True:
            await self._sleep(self.backoff_sec())
            await self.tick_safely()

    async def _fail_interrupted(self) -> None:
        # A claim held when the last process died can never resolve: its run died with
        # the process. Failing it outright, not retrying, because the robot may already
        # have started it before the restart.
        await self._occurrences.fail_claimed(at=self._now(), reason="interrupted by a restart")
        _LOGGER.info("claimed occurrences left by the previous process marked failed")

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
            # The slot is taken before the readiness check and held until the clean has
            # started, so nothing else can start a run in the await between the two.
            # Gated per Occurrence, not per tick: the first firing makes the robot busy
            # or holds the slot, so the second of two simultaneous ones waits.
            held = self._run_slot.acquire()
            launched = False
            try:
                reason = readiness_block(
                    await self._robot.get_status(), run_in_flight=not held
                )
                if reason is not None:
                    await self._recorder.blocked(occurrence, reason)
                    continue
                if not await self._occurrences.claim(occurrence.id):
                    continue
                action = actions[occurrence.rule_id]
                try:
                    if isinstance(action, SequenceAction):
                        await self._launch(occurrence, action)
                        launched = True
                    else:
                        await fire(
                            self._robot,
                            cleaning_log=self._cleaning_log,
                            action=action,
                            now=self._now,
                        )
                except Exception as exc:  # noqa: BLE001 - a refusal is an outcome, not a crash
                    await self._recorder.failed_attempt(occurrence, str(exc), self._now())
                else:
                    if not launched:
                        await self._recorder.fired(occurrence, self._now())
            finally:
                # A launched run owns the slot now and releases it when it ends.
                if held and not launched:
                    self._run_slot.release()

    async def _launch(self, occurrence: Occurrence, action: SequenceAction) -> None:
        """Start the Sequence in the background and return without waiting on it. The
        Occurrence is fired once the robot first departs; until then it stays claimed."""
        sequence = await self._sequences.get_sequence(action.sequence_id)
        if sequence is None:
            raise LookupError(f"no Sequence {action.sequence_id}")
        departed = False

        async def on_first_departure() -> None:
            nonlocal departed
            # Set before marking: if the mark fails, a later Step's failure must still
            # not count as a failed attempt, which would re-fire Step 1.
            departed = True
            try:
                await self._recorder.fired(occurrence, self._now())
            except Exception:
                _LOGGER.exception(
                    "occurrence=%d rule=%d could not be marked fired;"
                    " it stays claimed and is not re-fired",
                    occurrence.id, occurrence.rule_id,
                )

        async def on_end(error: Exception | None) -> None:
            if departed or error is None:
                return
            if isinstance(error, RunCancelled):
                await self._recorder.cancelled(occurrence, self._now())
            else:
                await self._recorder.failed_attempt(occurrence, str(error), self._now())

        self._launcher.launch(
            sequence.steps,
            name=sequence.name,
            sequence_id=sequence.id,
            source=CleanSource.SCHEDULER,
            on_first_departure=on_first_departure,
            on_end=on_end,
        )
