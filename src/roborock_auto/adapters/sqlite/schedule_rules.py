"""ScheduleRulePort over the schedule_rules table.

Every row carries exactly one device identifier and the store only reads its own device's
rows (docs/adr/0005). The Recurrence is stored as a comma-separated Weekday list plus an
ISO wall-clock time; the Action as a type discriminator plus a JSON payload, parsed back
into its typed value object on read so a malformed row fails loudly here rather than
leaking a dictionary into the domain (docs/adr/0004). Deleting flags the row instead of
removing it, so a Rule's history survives its deletion.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import time

from roborock_auto.domain.models import (
    Action,
    CleanAction,
    CleaningMode,
    ScheduleRule,
    Weekday,
    WeeklyGrid,
)

_CLEAN = "clean"


def _encode_action(action: Action) -> tuple[str, str]:
    segments = None if action.segment_ids is None else list(action.segment_ids)
    return _CLEAN, json.dumps({"mode": action.mode.value, "segment_ids": segments})


def _parse_action(action_type: str, action_json: str) -> Action:
    if action_type != _CLEAN:
        raise ValueError(f"unknown action type {action_type!r} in schedule_rules")
    payload = json.loads(action_json)
    segments = payload["segment_ids"]
    return CleanAction(
        mode=CleaningMode(payload["mode"]),
        segment_ids=None if segments is None else tuple(segments),
    )


def _encode_days(days: frozenset[Weekday]) -> str:
    return ",".join(day.value for day in Weekday if day in days)


def _rule(row: sqlite3.Row | tuple) -> ScheduleRule:
    rule_id, name, days, at_time, action_type, action_json, enabled = row
    return ScheduleRule(
        id=rule_id,
        name=name,
        recurrence=WeeklyGrid(
            days=frozenset(Weekday(day) for day in days.split(",")),
            at=time.fromisoformat(at_time),
        ),
        action=_parse_action(action_type, action_json),
        enabled=bool(enabled),
    )


class SqliteScheduleRules:
    def __init__(self, conn: sqlite3.Connection, device_id: str) -> None:
        self._conn = conn
        self._device_id = device_id

    async def create_rule(
        self, name: str, recurrence: WeeklyGrid, action: Action, enabled: bool
    ) -> ScheduleRule:
        action_type, action_json = _encode_action(action)
        cursor = self._conn.execute(
            "INSERT INTO schedule_rules"
            " (device_id, name, days, at_time, action_type, action_json, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._device_id,
                name,
                _encode_days(recurrence.days),
                recurrence.at.isoformat(),
                action_type,
                action_json,
                int(enabled),
            ),
        )
        self._conn.commit()
        return ScheduleRule(
            id=cursor.lastrowid, name=name, recurrence=recurrence, action=action, enabled=enabled
        )

    async def list_rules(self) -> list[ScheduleRule]:
        rows = self._conn.execute(
            "SELECT id, name, days, at_time, action_type, action_json, enabled"
            " FROM schedule_rules WHERE device_id = ? AND deleted = 0 ORDER BY id",
            (self._device_id,),
        ).fetchall()
        self._conn.commit()
        return [_rule(row) for row in rows]

    async def replace_rule(self, rule: ScheduleRule) -> bool:
        action_type, action_json = _encode_action(rule.action)
        cursor = self._conn.execute(
            "UPDATE schedule_rules"
            " SET name = ?, days = ?, at_time = ?, action_type = ?, action_json = ?, enabled = ?"
            " WHERE device_id = ? AND id = ? AND deleted = 0",
            (
                rule.name,
                _encode_days(rule.recurrence.days),
                rule.recurrence.at.isoformat(),
                action_type,
                action_json,
                int(rule.enabled),
                self._device_id,
                rule.id,
            ),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    async def delete_rule(self, rule_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE schedule_rules SET deleted = 1"
            " WHERE device_id = ? AND id = ? AND deleted = 0",
            (self._device_id, rule_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1
