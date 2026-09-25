"""Starting a saved Sequence as a background run in the RunSlot.

Every entry point that runs a Sequence (the web endpoint and the Scheduler) goes through
here, so a run started anywhere writes the same progress into the slot, is cancellable
through the slot's cancel event, and is the slot's task that shutdown cancels.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from roborock_auto.application.retry import Sleep
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.sequence_runner import (
    Clock,
    Phase,
    RunCancelled,
    SequencePorts,
    run_sequence,
)
from roborock_auto.application.start_clean import utc_now
from roborock_auto.config import Config
from roborock_auto.domain.models import CleanSource, Sequence
from roborock_auto.ports.cleaning_log import CleaningLogPort

OnEnd = Callable[[Exception | None], Awaitable[None]]

_PHASE_LABELS = {
    Phase.SETTING_UP: "setting carpet and cleaning mode",
    Phase.DEPARTING: "waiting for the robot to leave the dock",
    Phase.CLEANING: "cleaning, waiting for the robot to return",
    Phase.DONE: "done",
}


class SequenceLauncher:
    def __init__(
        self,
        gw: SequencePorts,
        config: Config,
        *,
        slot: RunSlot,
        cleaning_log: CleaningLogPort,
        now: Callable[[], datetime] = utc_now,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self._gw = gw
        self._config = config
        self._slot = slot
        self._cleaning_log = cleaning_log
        self._now = now
        self._clock = clock
        self._sleep = sleep
        self._log = log

    def launch(
        self,
        sequence: Sequence,
        *,
        source: CleanSource,
        on_first_departure: Callable[[], Awaitable[None]] | None = None,
        on_end: OnEnd | None = None,
    ) -> asyncio.Task:
        """Run `sequence` as a background task. The caller has already acquired the
        slot; the run releases it when it ends. `on_end` receives the error that ended
        the run (RunCancelled for a cancel), or None on success."""
        slot = self._slot
        progress = slot.progress
        count = len(sequence.steps)
        progress.update(
            stage="running",
            message=f"Step 1 of {count}: {_PHASE_LABELS[Phase.SETTING_UP]}",
            error=None,
            sequence_id=sequence.id,
            sequence_name=sequence.name,
            source=source.value,
            step=0,
            step_count=count,
            phase=Phase.SETTING_UP.value,
        )

        def on_progress(index: int, phase: Phase) -> None:
            progress.update(
                step=index,
                phase=phase.value,
                message=f"Step {index + 1} of {count}: {_PHASE_LABELS[phase]}",
            )

        async def run() -> None:
            try:
                error = await attempt()
                if on_end is not None:
                    await on_end(error)
            finally:
                slot.release()

        async def attempt() -> Exception | None:
            try:
                await run_sequence(
                    self._gw,
                    sequence.steps,
                    self._config,
                    cleaning_log=self._cleaning_log,
                    source=source,
                    clock=self._clock,
                    sleep=self._sleep,
                    now=self._now,
                    log=self._log,
                    on_progress=on_progress,
                    on_first_departure=on_first_departure,
                    cancel=slot.cancel,
                )
            except RunCancelled as exc:
                progress.update(stage="cancelled", message="Sequence cancelled", error=None)
                return exc
            except Exception as exc:  # noqa: BLE001 - surfaced through progress and on_end
                progress.update(stage="error", message=str(exc), error=str(exc))
                return exc
            progress.update(stage="done", message="Sequence complete", error=None)
            return None

        slot.task = asyncio.create_task(run())
        return slot.task
