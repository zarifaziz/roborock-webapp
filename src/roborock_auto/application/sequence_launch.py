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
from enum import Enum

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
from roborock_auto.domain.models import CleanSource, Step
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.setup_settings import SetupSettingsPort

OnEnd = Callable[[Exception | None], Awaitable[None]]


def step_body(step: Step) -> dict:
    """The one wire shape of a Step, shared by the Sequence and Up next endpoints and by
    run progress, so the UI renders a Step the same way wherever it appears."""
    return {
        "mode": step.mode.value,
        "carpet": step.carpet.value,
        "segment_ids": None if step.segment_ids is None else list(step.segment_ids),
        "repeat": step.repeat,
        # null is "Same as Setup".
        "fan_power": _value(step.settings.fan_power),
        "water_mode": _value(step.settings.water_mode),
        "mop_route": _value(step.settings.mop_route),
    }


def _value(setting: Enum | None) -> str | None:
    return None if setting is None else setting.value


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
        setup_store: SetupSettingsPort,
        now: Callable[[], datetime] = utc_now,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self._gw = gw
        self._config = config
        self._slot = slot
        self._cleaning_log = cleaning_log
        self._setup_store = setup_store
        self._now = now
        self._clock = clock
        self._sleep = sleep
        self._log = log

    def launch(
        self,
        steps: tuple[Step, ...],
        *,
        name: str,
        sequence_id: int | None,
        source: CleanSource,
        on_first_departure: Callable[[], Awaitable[None]] | None = None,
        on_end: OnEnd | None = None,
    ) -> asyncio.Task:
        """Run `steps` as a background task under `name`. `sequence_id` names the saved
        Sequence the Steps came from, or None for an ad-hoc run from Up next. The caller
        has already acquired the slot; the run releases it when it ends. `on_end`
        receives the error that ended the run (RunCancelled for a cancel), or None on
        success."""
        slot = self._slot
        progress = slot.progress
        count = len(steps)
        progress.update(
            stage="running",
            message=f"Step 1 of {count}: {_PHASE_LABELS[Phase.SETTING_UP]}",
            error=None,
            sequence_id=sequence_id,
            sequence_name=name,
            source=source.value,
            step=0,
            step_count=count,
            steps=[step_body(step) for step in steps],
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
                    steps,
                    self._config,
                    cleaning_log=self._cleaning_log,
                    setup_store=self._setup_store,
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
