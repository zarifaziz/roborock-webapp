"""CleaningLogPort over the cleaning_runs table.

Every row carries exactly one device identifier and the log only reads its own device's
rows (docs/adr/0005). Timestamps are stored as UTC ISO-8601 text; segment lists as a
JSON array, NULL meaning whole house.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

from roborock_auto.domain.models import CleaningMode, CleaningRun, CleanSource


class SqliteCleaningLog:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def record_run(self, run: CleaningRun) -> None:
        if run.started_at.tzinfo is None:
            raise ValueError("CleaningRun.started_at must be timezone-aware")
        segments = None if run.segment_ids is None else json.dumps(list(run.segment_ids))
        self._conn.execute(
            "INSERT INTO cleaning_runs (device_id, source, mode, segment_ids, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                self._device_id,
                run.source.value,
                run.mode.value,
                segments,
                run.started_at.astimezone(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    async def list_runs(self) -> list[CleaningRun]:
        rows = self._conn.execute(
            "SELECT source, mode, segment_ids, started_at FROM cleaning_runs"
            " WHERE device_id = ? ORDER BY started_at, id",
            (self._device_id,),
        ).fetchall()
        self._conn.commit()
        return [
            CleaningRun(
                source=CleanSource(source),
                mode=CleaningMode(mode),
                segment_ids=None if segments is None else tuple(json.loads(segments)),
                started_at=datetime.fromisoformat(started_at),
            )
            for source, mode, segments, started_at in rows
        ]

    async def last_cleaned(self, segment_ids: Iterable[int]) -> dict[int, datetime]:
        # MAX(started_at) per requested segment (docs/adr/0003), folded in Python because
        # segment lists live as JSON on the run row. A whole-house run stores NULL, so it
        # counts towards every requested segment.
        wanted = frozenset(segment_ids)
        latest: dict[int, datetime] = {}
        for run in await self.list_runs():
            covered = wanted if run.segment_ids is None else wanted.intersection(run.segment_ids)
            for segment_id in covered:
                if segment_id not in latest or run.started_at > latest[segment_id]:
                    latest[segment_id] = run.started_at
        return latest
