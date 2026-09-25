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

from roborock_auto.domain.models import CarpetMode, CleaningMode, CleaningRun, CleanSource
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.robot import CleaningControlPort, CleaningSettingsPort, RobotStatusPort


class CleanPorts(CleaningControlPort, CleaningSettingsPort, RobotStatusPort, Protocol):
    """Everything this use case needs from the robot — and nothing more."""


class SettingsNotConfirmed(RuntimeError):
    """The robot did not report the settings requested for a new clean."""


def utc_now() -> datetime:
    return datetime.now(UTC)


async def verify_cleaning_mode(gw: RobotStatusPort, mode: CleaningMode) -> None:
    status = await gw.get_status()
    if status.cleaning_mode != mode.value:
        raise SettingsNotConfirmed(
            f"Cleaning mode not confirmed: requested {mode.value}, "
            f"robot reported {status.cleaning_mode or 'unknown'}. Clean not started."
        )


async def start_clean(
    gw: CleanPorts,
    *,
    cleaning_log: CleaningLogPort,
    source: CleanSource,
    mode: CleaningMode,
    segment_ids: list[int] | None,
    repeat: int = 1,
    carpet_mode: CarpetMode | None = None,
    now: Callable[[], datetime] = utc_now,
) -> None:
    if segment_ids is not None and not segment_ids:
        raise ValueError("segment_ids must be None (whole house) or name at least one segment")
    if carpet_mode is CarpetMode.UNKNOWN:
        raise ValueError("cannot set carpet mode to 'unknown'")
    await gw.set_cleaning_mode(mode)
    if carpet_mode is not None:
        await gw.set_carpet_mode(carpet_mode)
    await verify_cleaning_mode(gw, mode)
    if carpet_mode is not None:
        reported_carpet = await gw.get_carpet_mode()
        if reported_carpet is not carpet_mode:
            raise SettingsNotConfirmed(
                f"Carpet mode not confirmed: requested {carpet_mode.value}, "
                f"robot reported {reported_carpet.value}. Clean not started."
            )
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
