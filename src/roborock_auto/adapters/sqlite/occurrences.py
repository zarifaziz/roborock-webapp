"""OccurrencePort over the occurrences table.

Every row carries exactly one device identifier and the store only reads its own device's
rows (docs/adr/0005). Timestamps are stored as UTC ISO-8601 text. The claim is a
conditional UPDATE of a PENDING row to CLAIMED committed immediately: the database's own
locking makes exactly one of two concurrent claimers see a row count of one, which is the
right to fire (docs/adr/0002). Reads join the rule's current name so the timeline can
label every Occurrence, including those of a soft-deleted Rule.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from roborock_auto.domain.models import Occurrence, OccurrenceState


def _utc_text(name: str, value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError(f"Occurrence.{name} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class SqliteOccurrences:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def add_pending(
        self, rule_id: int, scheduled_for: datetime, deadline: datetime
    ) -> None:
        # OR IGNORE against the (device, rule, instant) unique index is what makes
        # materialisation idempotent: a live row at the same instant wins, silently.
        self._conn.execute(
            "INSERT OR IGNORE INTO occurrences"
            " (device_id, rule_id, scheduled_for, deadline, state)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                self._device_id,
                rule_id,
                _utc_text("scheduled_for", scheduled_for),
                _utc_text("deadline", deadline),
                OccurrenceState.PENDING.value,
            ),
        )
        self._conn.commit()

    async def claim(self, occurrence_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE occurrences SET state = ?"
            " WHERE device_id = ? AND id = ? AND state = ?",
            (
                OccurrenceState.CLAIMED.value,
                self._device_id,
                occurrence_id,
                OccurrenceState.PENDING.value,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    async def cancel_pending(self, rule_id: int) -> None:
        self._conn.execute(
            "UPDATE occurrences SET state = ?"
            " WHERE device_id = ? AND rule_id = ? AND state = ?",
            (
                OccurrenceState.CANCELLED.value,
                self._device_id,
                rule_id,
                OccurrenceState.PENDING.value,
            ),
        )
        self._conn.commit()

    async def mark_expired(self, occurrence_id: int) -> None:
        self._conn.execute(
            "UPDATE occurrences SET state = ?"
            " WHERE device_id = ? AND id = ? AND state = ?",
            (
                OccurrenceState.EXPIRED.value,
                self._device_id,
                occurrence_id,
                OccurrenceState.PENDING.value,
            ),
        )
        self._conn.commit()

    async def mark_blocked(self, occurrence_id: int, reason: str) -> None:
        self._conn.execute(
            "UPDATE occurrences SET blocked_reason = ?"
            " WHERE device_id = ? AND id = ? AND state = ?",
            (reason, self._device_id, occurrence_id, OccurrenceState.PENDING.value),
        )
        self._conn.commit()

    async def mark_fired(self, occurrence_id: int, fired_at: datetime) -> None:
        self._conn.execute(
            "UPDATE occurrences SET state = ?, fired_at = ?, blocked_reason = NULL"
            " WHERE device_id = ? AND id = ? AND state = ?",
            (
                OccurrenceState.FIRED.value,
                _utc_text("fired_at", fired_at),
                self._device_id,
                occurrence_id,
                OccurrenceState.CLAIMED.value,
            ),
        )
        self._conn.commit()

    async def record_failed_attempt(
        self, occurrence_id: int, *, at: datetime, reason: str, exhausted: bool
    ) -> None:
        state = OccurrenceState.FAILED if exhausted else OccurrenceState.PENDING
        self._conn.execute(
            "UPDATE occurrences SET state = ?, attempts = attempts + 1,"
            " last_attempt_at = ?, blocked_reason = ?"
            " WHERE device_id = ? AND id = ? AND state = ?",
            (
                state.value,
                _utc_text("last_attempt_at", at),
                reason,
                self._device_id,
                occurrence_id,
                OccurrenceState.CLAIMED.value,
            ),
        )
        self._conn.commit()

    async def fail_claimed(self, *, at: datetime, reason: str) -> None:
        self._conn.execute(
            "UPDATE occurrences SET state = ?, attempts = attempts + 1,"
            " last_attempt_at = ?, blocked_reason = ?"
            " WHERE device_id = ? AND state = ?",
            (
                OccurrenceState.FAILED.value,
                _utc_text("last_attempt_at", at),
                reason,
                self._device_id,
                OccurrenceState.CLAIMED.value,
            ),
        )
        self._conn.commit()

    async def list_occurrences(self) -> list[Occurrence]:
        rows = self._conn.execute(
            "SELECT o.id, o.rule_id, r.name, o.scheduled_for, o.deadline, o.state,"
            " o.attempts, o.last_attempt_at, o.blocked_reason, o.fired_at"
            " FROM occurrences o"
            " JOIN schedule_rules r ON r.id = o.rule_id AND r.device_id = o.device_id"
            " WHERE o.device_id = ? ORDER BY o.scheduled_for, o.id",
            (self._device_id,),
        ).fetchall()
        self._conn.commit()
        return [
            Occurrence(
                id=row[0],
                rule_id=row[1],
                rule_name=row[2],
                scheduled_for=datetime.fromisoformat(row[3]),
                deadline=datetime.fromisoformat(row[4]),
                state=OccurrenceState(row[5]),
                attempts=row[6],
                last_attempt_at=None if row[7] is None else datetime.fromisoformat(row[7]),
                blocked_reason=row[8],
                fired_at=None if row[9] is None else datetime.fromisoformat(row[9]),
            )
            for row in rows
        ]
