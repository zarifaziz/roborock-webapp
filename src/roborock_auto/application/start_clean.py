"""The shared clean use case: set the cleaning mode, start the clean, record the run.

Every entry point that starts a plain Clean routes through here, so the cleaning log and
the source are required — a caller cannot start an anonymous clean and cannot skip
recording it (docs/adr/0003). The Two-Stage Clean is its own procedure and records its
passes itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from roborock_auto.domain.models import CleaningMode, CleaningRun, CleanSource
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.robot import CleaningControlPort, CleaningSettingsPort


class CleanPorts(CleaningControlPort, CleaningSettingsPort, Protocol):
    """Everything this use case needs from the robot — and nothing more."""


def utc_now() -> datetime:
    return datetime.now(UTC)


async def start_clean(
    gw: CleanPorts,
    *,
    cleaning_log: CleaningLogPort,
    source: CleanSource,
    mode: CleaningMode,
    segment_ids: list[int] | None,
    repeat: int = 1,
    now: Callable[[], datetime] = utc_now,
) -> None:
    if segment_ids is not None and not segment_ids:
        raise ValueError("segment_ids must be None (whole house) or name at least one segment")
    await gw.set_cleaning_mode(mode)
    if segment_ids is None:
        await gw.start()
    else:
        await gw.clean_rooms(segment_ids, repeat=repeat)
    # Recorded only once the robot accepted the commands: a rejected clean never happened.
    await cleaning_log.record_run(
        CleaningRun(
            source=source,
            mode=mode,
            segment_ids=None if segment_ids is None else tuple(segment_ids),
            started_at=now(),
        )
    )
