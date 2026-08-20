"""RoomIntervalPort over the room_intervals table.

Every row carries exactly one device identifier and the store only reads its own device's
rows (docs/adr/0005). Durations are stored as seconds, so a sub-day Interval needs no
special case.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from roborock_auto.domain.models import RoomInterval


class SqliteRoomIntervals:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def set_interval(self, interval: RoomInterval) -> None:
        self._conn.execute(
            "INSERT INTO room_intervals (device_id, segment_id, interval_seconds)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT (device_id, segment_id)"
            " DO UPDATE SET interval_seconds = excluded.interval_seconds",
            (self._device_id, interval.segment_id, interval.interval.total_seconds()),
        )
        self._conn.commit()

    async def list_intervals(self) -> list[RoomInterval]:
        rows = self._conn.execute(
            "SELECT segment_id, interval_seconds FROM room_intervals"
            " WHERE device_id = ? ORDER BY segment_id",
            (self._device_id,),
        ).fetchall()
        self._conn.commit()
        return [
            RoomInterval(segment_id=segment_id, interval=timedelta(seconds=seconds))
            for segment_id, seconds in rows
        ]

    async def remove_interval(self, segment_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM room_intervals WHERE device_id = ? AND segment_id = ?",
            (self._device_id, segment_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1
