"""SequencePort over the sequences table.

Every row carries exactly one device identifier and the store only reads its own device's
rows (docs/adr/0005). The Steps are stored as one JSON list and parsed back into typed
Steps on read, so a malformed row fails loudly here rather than leaking a dictionary into
the domain (docs/adr/0004). Ids come from AUTOINCREMENT so a deleted Sequence's id is
never handed to a new one that a stale client could then run or overwrite by mistake.
"""

from __future__ import annotations

import json
import sqlite3

from roborock_auto.domain.models import CarpetMode, CleaningMode, Sequence, Step


def _encode_steps(steps: tuple[Step, ...]) -> str:
    return json.dumps(
        [
            {
                "mode": step.mode.value,
                "carpet": step.carpet.value,
                "segment_ids": None if step.segment_ids is None else list(step.segment_ids),
                "repeat": step.repeat,
            }
            for step in steps
        ]
    )


def _parse_steps(steps_json: str) -> tuple[Step, ...]:
    return tuple(
        Step(
            mode=CleaningMode(payload["mode"]),
            carpet=CarpetMode(payload["carpet"]),
            segment_ids=None if payload["segment_ids"] is None else tuple(payload["segment_ids"]),
            # Rows written before Steps had passes carry no key and ran one pass.
            repeat=payload.get("repeat", 1),
        )
        for payload in json.loads(steps_json)
    )


def _sequence(row: tuple) -> Sequence:
    sequence_id, name, steps_json = row
    return Sequence(id=sequence_id, name=name, steps=_parse_steps(steps_json))


class SqliteSequences:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def create_sequence(self, name: str, steps: tuple[Step, ...]) -> Sequence:
        cursor = self._conn.execute(
            "INSERT INTO sequences (device_id, name, steps_json) VALUES (?, ?, ?)",
            (self._device_id, name, _encode_steps(steps)),
        )
        self._conn.commit()
        return Sequence(id=cursor.lastrowid, name=name, steps=steps)

    async def list_sequences(self) -> list[Sequence]:
        rows = self._conn.execute(
            "SELECT id, name, steps_json FROM sequences WHERE device_id = ? ORDER BY id",
            (self._device_id,),
        ).fetchall()
        self._conn.commit()
        return [_sequence(row) for row in rows]

    async def get_sequence(self, sequence_id: int) -> Sequence | None:
        row = self._conn.execute(
            "SELECT id, name, steps_json FROM sequences WHERE device_id = ? AND id = ?",
            (self._device_id, sequence_id),
        ).fetchone()
        self._conn.commit()
        return None if row is None else _sequence(row)

    async def replace_sequence(self, sequence: Sequence) -> bool:
        cursor = self._conn.execute(
            "UPDATE sequences SET name = ?, steps_json = ? WHERE device_id = ? AND id = ?",
            (sequence.name, _encode_steps(sequence.steps), self._device_id, sequence.id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    async def delete_sequence(self, sequence_id: int) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM sequences WHERE device_id = ? AND id = ?",
            (self._device_id, sequence_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1
