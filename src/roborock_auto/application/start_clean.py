"""The shared clean use case: set the cleaning mode, put Setup's fan, water and route
back, start the clean, record the run.

Every entry point that starts a plain Clean routes through here, so the cleaning log and
the source are required — a caller cannot start an anonymous clean and cannot skip
recording it (docs/adr/0003). The Two-Stage Clean is its own procedure and records its
passes itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleaningRun,
    CleanSettings,
    CleanSource,
)
from roborock_auto.domain.policies import (
    resolve_setup,
    settings_after_mode_switch,
    setup_settings,
)
from roborock_auto.ports.cleaning_log import CleaningLogPort
from roborock_auto.ports.robot import CleaningControlPort, CleaningSettingsPort, RobotStatusPort
from roborock_auto.ports.setup_settings import SetupSettingsPort

_LOGGER = logging.getLogger(__name__)


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


async def read_setup(gw: RobotStatusPort, store: SetupSettingsPort) -> CleanSettings:
    """Setup as it stands before a mode switch overwrites the robot's copy of it."""
    return resolve_setup(await store.get_setup_settings(), setup_settings(await gw.get_status()))


def settings_commands(
    gw: CleaningSettingsPort, settings: CleanSettings
) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
    """One (description, send) per setting to send, so each can fail on its own: a refused
    setting costs only itself, and the clean runs on with the mode's default for it."""
    commands: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    if settings.fan_power is not None:
        power = settings.fan_power
        commands.append((f"fan power to {power.value}", lambda: gw.set_fan_power(power)))
    if settings.water_mode is not None:
        water = settings.water_mode
        commands.append((f"water flow to {water.value}", lambda: gw.set_water_mode(water)))
    if settings.mop_route is not None:
        route = settings.mop_route
        commands.append((f"mop route to {route.value}", lambda: gw.set_mop_mode(route)))
    return commands


async def start_clean(
    gw: CleanPorts,
    *,
    cleaning_log: CleaningLogPort,
    setup_store: SetupSettingsPort,
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
    setup = await read_setup(gw, setup_store)
    await gw.set_cleaning_mode(mode)
    for what, send in settings_commands(gw, settings_after_mode_switch(mode, CleanSettings(), setup)):
        try:
            await send()
        except Exception as exc:  # noqa: BLE001 - a missing setting must not cost the clean
            _LOGGER.warning("could not set %s, cleaning with the mode's default: %s", what, exc)
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
