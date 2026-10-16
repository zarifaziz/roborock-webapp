"""The Two-Stage Clean: the two-Step Sequence preset that never wets carpet.

Vacuum with carpet Rise, wait for the robot to leave and come home, then mop with carpet
Avoid, both over the same Segments. It is built in code rather than stored, because its
Segments are whatever the caller picked for this run.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime

from roborock_auto.application.retry import Sleep
from roborock_auto.application.sequence_runner import (
    STATUS_FAULT_BUDGET,
    Clock,
    Phase,
    RunCancelled,
    SequencePorts,
    run_sequence,
    wait_for_departure,
    wait_for_return,
)
from roborock_auto.application.start_clean import utc_now
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, CleaningMode, CleanSource, Step
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.setup_settings import SetupSettingsPort

__all__ = [
    "STATUS_FAULT_BUDGET",
    "RunCancelled",
    "run_two_stage",
    "two_stage_steps",
    "wait_for_departure",
    "wait_for_return",
]

# The stage tokens the status endpoint and its UI already speak, per (Step, Phase).
_STAGES = {
    (0, Phase.SETTING_UP): "vacuuming",
    (0, Phase.DEPARTING): "vacuuming",
    (0, Phase.CLEANING): "vacuum_waiting",
    (1, Phase.SETTING_UP): "mopping",
    (1, Phase.DEPARTING): "mopping",
    (1, Phase.CLEANING): "mop_waiting",
}


def two_stage_steps(segment_ids: list[int] | None) -> tuple[Step, Step]:
    """The preset's two Steps over the same Segments: vacuum with Rise, mop with Avoid.
    The one definition, used by the run and by Up next, so a queued Two-Stage Clean can
    never drift from the one the button starts."""
    segments = tuple(segment_ids) if segment_ids else None
    return (
        Step(mode=CleaningMode.VACUUM, carpet=CarpetMode.RISE, segment_ids=segments),
        Step(mode=CleaningMode.MOP, carpet=CarpetMode.AVOID, segment_ids=segments),
    )


async def run_two_stage(
    gw: SequencePorts,
    config: Config,
    *,
    cleaning_log: CleaningLogPort,
    setup_store: SetupSettingsPort,
    source: CleanSource,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    now: Callable[[], datetime] = utc_now,
    interval: float | None = None,
    segment_ids: list[int] | None = None,
    log: Callable[[str], None] = print,
    on_stage: Callable[[str], None] = lambda _stage: None,
    cancel: asyncio.Event | None = None,
) -> None:
    steps = two_stage_steps(segment_ids)
    last_stage: str | None = None

    def on_progress(index: int, phase: Phase) -> None:
        nonlocal last_stage
        stage = _STAGES.get((index, phase))
        if stage is not None and stage != last_stage:
            last_stage = stage
            on_stage(stage)

    await run_sequence(
        gw,
        steps,
        config,
        cleaning_log=cleaning_log,
        setup_store=setup_store,
        source=source,
        clock=clock,
        sleep=sleep,
        now=now,
        interval=interval,
        log=log,
        on_progress=on_progress,
        cancel=cancel,
    )
    on_stage("done")
