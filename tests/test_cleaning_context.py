import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from roborock_auto.adapters.web import cleaning_context
from roborock_auto.adapters.web.cleaning_context import RecentCleaning, assistant_evidence
from roborock_auto.domain.cleaning_history import CleanRecord
from roborock_auto.domain.models import CleaningMode, CleaningRun, CleanSource


async def test_recent_history_keeps_started_requests_distinct_from_completed_records():
    now = datetime(2026, 9, 25, 6, tzinfo=UTC)
    gateway = SimpleNamespace(get_last_clean_record=AsyncMock(return_value=None))
    log = SimpleNamespace(list_runs=AsyncMock(return_value=[
        CleaningRun(CleanSource.APP, CleaningMode.VACUUM, (7,), now),
    ]))
    history = RecentCleaning(gateway, log)
    responses = await asyncio.gather(*(history.read() for _ in range(3)))
    expected = {
        'last_record': None, 'record_available': True, 'record_stale': False,
        'recent_requests': [{
            'started_at': now.isoformat(), 'mode': 'vacuum', 'segment_ids': [7], 'source': 'app',
        }],
    }
    assert responses == [expected] * 3
    assert gateway.get_last_clean_record.await_count == 1


async def test_history_outage_is_explicit_and_coalesced():
    gateway = SimpleNamespace(get_last_clean_record=AsyncMock(side_effect=TimeoutError))
    log = SimpleNamespace(list_runs=AsyncMock(return_value=[]))
    history = RecentCleaning(gateway, log)
    responses = await asyncio.gather(history.read(), history.read())
    assert responses == [{
        'last_record': None, 'record_available': False, 'record_stale': True,
        'recent_requests': [],
    }] * 2
    assert gateway.get_last_clean_record.await_count == 1


async def test_later_outage_retains_record_and_labels_evidence_stale(monkeypatch):
    clock = SimpleNamespace(value=100)
    monkeypatch.setattr(cleaning_context, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    now = datetime(2026, 9, 25, 6, tzinfo=UTC)
    gateway = SimpleNamespace(get_last_clean_record=AsyncMock(side_effect=[
        CleanRecord(now, now, 600, 12.4, 'completed', 0), TimeoutError(),
    ]))
    log = SimpleNamespace(list_runs=AsyncMock(return_value=[]))
    history = RecentCleaning(gateway, log)
    await history.read()
    clock.value += 61
    recent = await history.read()
    assert recent == {
        'last_record': {'started_at': now.isoformat(), 'ended_at': now.isoformat(),
                        'duration_s': 600, 'area_m2': 12.4, 'outcome': 'completed', 'error_code': 0},
        'record_available': True, 'record_stale': True, 'recent_requests': [],
    }
    assert assistant_evidence(recent) == [{
        **recent['last_record'], 'id': 'robot:latest', 'type': 'robot_record', 'stale': True,
    }]
