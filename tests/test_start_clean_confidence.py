"""A start is sent only after the requested settings have been read back."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.application.start_clean import start_clean
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleanSource,
    DeviceStatus,
    FanPower,
)


async def test_per_run_carpet_is_applied_and_verified_before_start():
    gateway = FakeRobot()
    log = AsyncMock()
    now = datetime(2026, 9, 25, tzinfo=UTC)
    await start_clean(gateway, cleaning_log=log, source=CleanSource.APP,
                      mode=CleaningMode.VACUUM, carpet_mode=CarpetMode.RISE,
                      segment_ids=[7], repeat=2, now=lambda: now)
    assert gateway.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_fan_power", FanPower.BALANCED),  # Setup's fan, kept across the mode switch
        ("set_carpet_mode", CarpetMode.RISE),
        ("clean_rooms", (7,), 2),
    ]
    assert (await gateway.get_status()).cleaning_mode == "vacuum"
    assert await gateway.get_carpet_mode() is CarpetMode.RISE
    assert log.record_run.await_count == 1


@pytest.mark.parametrize("reported_mode", ["vac_and_mop", None])
async def test_unconfirmed_mode_never_starts_or_records(reported_mode):
    gateway = FakeRobot()
    gateway.get_status = AsyncMock(return_value=DeviceStatus(
        "charging", 100, False, True, cleaning_mode=reported_mode,
    ))
    log = AsyncMock()
    with pytest.raises(RuntimeError, match="Cleaning mode"):
        await start_clean(gateway, cleaning_log=log, source=CleanSource.APP,
                          mode=CleaningMode.VACUUM, segment_ids=[7])
    assert gateway.commands == [("set_cleaning_mode", CleaningMode.VACUUM)]
    log.record_run.assert_not_awaited()


async def test_unconfirmed_carpet_never_starts_or_records():
    gateway = FakeRobot()
    gateway.get_carpet_mode = AsyncMock(return_value=CarpetMode.AVOID)
    log = AsyncMock()
    with pytest.raises(RuntimeError, match="Carpet mode"):
        await start_clean(gateway, cleaning_log=log, source=CleanSource.APP,
                          mode=CleaningMode.VACUUM, carpet_mode=CarpetMode.RISE,
                          segment_ids=[7])
    assert gateway.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM), ("set_fan_power", FanPower.BALANCED),
        ("set_carpet_mode", CarpetMode.RISE),
    ]
    log.record_run.assert_not_awaited()


async def test_sequence_never_departs_with_unconfirmed_cleaning_mode(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Australia/Sydney")
    from roborock_auto.application.sequence_runner import run_sequence
    from roborock_auto.config import Config
    from roborock_auto.domain.models import Step

    gateway = FakeRobot()
    gateway.get_status = AsyncMock(return_value=DeviceStatus(
        "charging", 100, False, True, cleaning_mode="vac_and_mop",
    ))
    log = AsyncMock()
    steps = (Step(CleaningMode.VACUUM, CarpetMode.RISE, None),)
    with pytest.raises(RuntimeError, match="Cleaning mode"):
        await run_sequence(gateway, steps, Config.from_env(), cleaning_log=log, source=CleanSource.APP,
                           clock=iter(range(0, 100000, 10)).__next__, sleep=AsyncMock())
    assert gateway.commands == [
        ("set_carpet_mode", CarpetMode.RISE), ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_carpet_mode", CarpetMode.AVOID), ("dock",),
    ]
    log.record_run.assert_not_awaited()


async def test_concurrent_start_is_rejected_while_first_settings_are_applying():
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException, Request

    from roborock_auto.adapters.web import server
    from roborock_auto.application.run_slot import RunSlot

    gateway = FakeRobot()
    entered, release = asyncio.Event(), asyncio.Event()
    original = gateway.set_cleaning_mode

    async def slow_set(mode):
        entered.set()
        await release.wait()
        await original(mode)

    gateway.set_cleaning_mode = slow_set
    state = SimpleNamespace(gateway=gateway, run_slot=RunSlot(), cleaning_log=AsyncMock(),
                            now=lambda: datetime(2026, 9, 25, tzinfo=UTC))
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    first = asyncio.create_task(server.start(request, "vacuum", "rise"))
    await entered.wait()
    with pytest.raises(HTTPException) as error:
        await server.start(request, "mop", "avoid")
    assert error.value.status_code == 409
    release.set()
    assert await first == {"ok": True}
    assert gateway.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM), ("set_fan_power", FanPower.BALANCED),
        ("set_carpet_mode", CarpetMode.RISE), ("start",),
    ]
    assert state.run_slot.running is False
