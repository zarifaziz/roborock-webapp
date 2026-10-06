"""Up next: the ordered list of Cleans waiting for the robot.

The user asks for a Clean while the robot is out; instead of a refusal the request waits
here and starts once the robot is home, idle and error-free and nothing else holds the
RunSlot. Every entry runs through the Sequence runner, so a queued Clean gets the same
carpet read-back, departure confirmation and return wait as a Sequence.

The queue advances only after a success. A failed or cancelled run, or the user sending
the robot home, puts it on hold: the robot must never leave the dock again on its own
after the user stopped it. The user resumes or clears it.

State lives in memory, like run progress. A restart drops the list rather than sending
the robot out later for a request the user may have forgotten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from roborock_auto.application.retry import Sleep
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.scheduler import readiness_block
from roborock_auto.application.sequence_launch import SequenceLauncher
from roborock_auto.application.sequence_runner import CONFIRM_SAMPLES, RunCancelled
from roborock_auto.domain.models import CleanSource, QueuedClean, Step
from roborock_auto.ports.robot import RobotStatusPort

STOPPED_BY_USER = "you sent the robot home"
LAST_RUN_CANCELLED = "the last clean was cancelled"
LAST_RUN_FAILED = "the last clean failed"


class CleanQueue:
    def __init__(
        self,
        gw: RobotStatusPort,
        *,
        slot: RunSlot,
        launcher: SequenceLauncher,
        interval: float,
        sleep: Sleep = asyncio.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self._gw = gw
        self._slot = slot
        self._launcher = launcher
        self._interval = interval
        self._sleep = sleep
        self._log = log
        self.entries: list[QueuedClean] = []
        self.held = False
        self.hold_reason: str | None = None
        # Why the head entry has not started yet, for the UI; None once it is running.
        self.waiting_for: str | None = None
        self._next_id = 1
        self._task: asyncio.Task | None = None
        # Bumped by every resume, so a run that ends after one cannot re-hold the list.
        self._generation = 0

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    def add(
        self, label: str, steps: tuple[Step, ...], *, sequence_id: int | None = None
    ) -> QueuedClean:
        entry = QueuedClean(id=self._next_id, label=label, steps=steps, sequence_id=sequence_id)
        self._next_id += 1
        self.entries.append(entry)
        self._wake()
        return entry

    def remove(self, entry_id: int) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.id != entry_id]
        if not self.entries:
            self._settle()
        return len(self.entries) < before

    def move(self, entry_id: int, to: int) -> bool:
        """Put the entry at position `to` (0 = next), clamped to the list."""
        index = next((i for i, e in enumerate(self.entries) if e.id == entry_id), None)
        if index is None:
            return False
        entry = self.entries.pop(index)
        self.entries.insert(max(0, min(to, len(self.entries))), entry)
        return True

    def clear(self) -> None:
        self.entries.clear()
        self._settle()

    def hold(self, reason: str) -> None:
        """Stop advancing. A no-op with nothing queued, so an empty queue never shows a
        hold the user cannot act on. The drain loop sees the flag on its next poll."""
        if not self.entries:
            return
        if not self.held:
            # The first reason is the user's own action when there is one; the run
            # ending afterwards must not overwrite it with a generic one.
            self.hold_reason = reason
        self.held = True
        self.waiting_for = None

    def resume(self) -> None:
        self.held = False
        self.hold_reason = None
        self._generation += 1
        self._wake()

    def _settle(self) -> None:
        self.held = False
        self.hold_reason = None
        self.waiting_for = None

    def _wake(self) -> None:
        if self.held or not self.entries:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())
            self._task.add_done_callback(self._report_exit)

    def _report_exit(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._log(f"up next: drain loop died: {error!r}")

    async def _drain(self) -> None:
        ready_seen = 0
        while self.entries and not self.held:
            reason = await self._block_reason()
            if reason is not None:
                ready_seen = 0
                self.waiting_for = reason
                await self._sleep(self._interval)
                continue
            # Readiness must hold across consecutive polls, as the runner demands of
            # departure and return, so one stale read between dock servicing and the
            # next job does not send the robot out early.
            ready_seen += 1
            if ready_seen < CONFIRM_SAMPLES:
                await self._sleep(self._interval)
                continue
            ready_seen = 0
            # The user may have held, cleared or emptied the list during the status
            # read above; nothing must leave the dock on a decision taken before that.
            if self.held or not self.entries:
                break
            if not self._slot.acquire():
                continue
            entry = self.entries.pop(0)
            self.waiting_for = None
            generation = self._generation
            self._log(f"up next: starting {entry.label!r}, {len(self.entries)} still queued")
            error = await self._run(entry)
            if error is not None:
                reason = LAST_RUN_CANCELLED if isinstance(error, RunCancelled) else LAST_RUN_FAILED
                self._log(f"up next: {entry.label!r} ended ({reason}); holding the queue")
                # A Resume tapped while the run was still winding down must not be undone
                # by the hold that run's end would otherwise apply.
                if generation == self._generation:
                    self.hold(reason)

    async def _run(self, entry: QueuedClean) -> Exception | None:
        """Run one entry to its end and return the error that ended it, or None."""
        outcome: list[Exception | None] = []

        async def on_end(error: Exception | None) -> None:
            outcome.append(error)

        task = self._launcher.launch(
            entry.steps,
            name=entry.label,
            sequence_id=entry.sequence_id,
            source=CleanSource.APP,
            on_end=on_end,
        )
        # Shielded: the run belongs to the slot, not to this loop. Cancelling the loop
        # must not abort a robot mid-floor; shutdown cancels the slot's task itself.
        await asyncio.shield(task)
        return outcome[0] if outcome else None

    async def _block_reason(self) -> str | None:
        if self._slot.running:
            return "another run is in flight"
        try:
            status = await self._gw.get_status()
        except Exception as exc:  # noqa: BLE001 - a transport blip is a reason to wait, not to crash
            return f"no contact with the robot: {exc}"
        return readiness_block(status, run_in_flight=False)
