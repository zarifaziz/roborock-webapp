"""Read-only cleaning evidence shared by the UI and language assistant."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from roborock_auto.ports.cleaning_history import CleaningHistoryPort
from roborock_auto.ports.cleaning_log import CleaningLogPort

_LOGGER = logging.getLogger(__name__)


class RecentCleaning:
    def __init__(self, gateway: CleaningHistoryPort, cleaning_log: CleaningLogPort):
        self.gateway = gateway
        self.cleaning_log = cleaning_log
        self._lock = asyncio.Lock()
        self._at: float | None = None
        self._record: dict | None = None
        self._available = False
        self._stale = False

    async def read(self) -> dict:
        async with self._lock:
            if self._at is None or time.monotonic() - self._at >= 60:
                try:
                    record = await self.gateway.get_last_clean_record()
                    self._record = dataclasses.asdict(record) if record else None
                    if self._record:
                        for key in ('started_at', 'ended_at'):
                            value = self._record[key]
                            self._record[key] = value.isoformat() if value else None
                    self._available, self._stale = True, False
                except Exception:
                    _LOGGER.warning('Cleaning history refresh unavailable', exc_info=True)
                    self._stale = True
                self._at = time.monotonic()
            runs = sorted(await self.cleaning_log.list_runs(),
                          key=lambda run: run.started_at, reverse=True)[:20]
            return {
                'last_record': self._record,
                'record_available': self._available,
                'record_stale': self._stale,
                'recent_requests': [{
                    'started_at': run.started_at.isoformat(),
                    'mode': run.mode.value,
                    'segment_ids': list(run.segment_ids) if run.segment_ids is not None else None,
                    'source': run.source.value,
                } for run in runs],
            }


def assistant_evidence(recent: dict) -> list[dict]:
    evidence = []
    if recent['last_record']:
        evidence.append({
            **recent['last_record'], 'id': 'robot:latest', 'type': 'robot_record',
            'stale': recent['record_stale'],
        })
    for index, run in enumerate(recent['recent_requests']):
        evidence.append({**run, 'id': f'app:{index}', 'type': 'app_request'})
    return evidence
