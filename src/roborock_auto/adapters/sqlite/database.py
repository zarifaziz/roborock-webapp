"""Opening the app's SQLite database and migrating it forward.

Migrations are an append-only list of scripts; PRAGMA user_version is the cursor over
that list. Each pending script and its cursor advance commit in one transaction:
committing the schema before the cursor would leave a database that re-runs its own
migration on the next boot and fails there permanently.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE cleaning_runs (
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        source TEXT NOT NULL,
        mode TEXT NOT NULL,
        segment_ids TEXT,
        started_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE room_intervals (
        device_id TEXT NOT NULL,
        segment_id INTEGER NOT NULL,
        interval_seconds REAL NOT NULL,
        PRIMARY KEY (device_id, segment_id)
    );
    """,
    """
    CREATE TABLE schedule_rules (
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        name TEXT NOT NULL,
        recurrence_type TEXT NOT NULL CHECK (recurrence_type IN ('weekly_grid')),
        recurrence_json TEXT NOT NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('clean')),
        action_json TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    );
    """,
    # The unique key excludes CANCELLED rows: a cancelled Occurrence is a superseded
    # intent kept for history, and an edited Rule that restates the same instant must be
    # able to materialise it afresh. Every live or fired state keeps the key unique, so
    # materialisation stays idempotent and a fired instant can never fire twice.
    """
    CREATE TABLE occurrences (
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        rule_id INTEGER NOT NULL,
        scheduled_for TEXT NOT NULL,
        deadline TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN ('pending', 'claimed', 'fired', 'failed', 'expired', 'cancelled')
        ),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT,
        blocked_reason TEXT,
        fired_at TEXT
    );
    CREATE UNIQUE INDEX occurrences_rule_instant
        ON occurrences (device_id, rule_id, scheduled_for)
        WHERE state <> 'cancelled';
    """,
    # SQLite cannot alter a CHECK in place, so schedule_rules is rebuilt to admit the
    # Sequence Action. Ids are copied as-is: Occurrences join on rule_id.
    """
    CREATE TABLE sequences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        name TEXT NOT NULL,
        steps_json TEXT NOT NULL
    );
    CREATE TABLE schedule_rules_v5 (
        id INTEGER PRIMARY KEY,
        device_id TEXT NOT NULL,
        name TEXT NOT NULL,
        recurrence_type TEXT NOT NULL CHECK (recurrence_type IN ('weekly_grid')),
        recurrence_json TEXT NOT NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('clean', 'sequence')),
        action_json TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO schedule_rules_v5
        (id, device_id, name, recurrence_type, recurrence_json,
         action_type, action_json, enabled, deleted)
        SELECT id, device_id, name, recurrence_type, recurrence_json,
               action_type, action_json, enabled, deleted
        FROM schedule_rules;
    DROP TABLE schedule_rules;
    ALTER TABLE schedule_rules_v5 RENAME TO schedule_rules;
    """,
)


def open_database(path: str) -> sqlite3.Connection:
    # autocommit=False keeps every statement — DDL included — inside the explicit
    # transaction, which is what makes a migration script + version bump atomic.
    # check_same_thread=False: the connection is created in the lifespan handler but
    # used from whichever thread runs the event loop.
    conn = sqlite3.connect(path, autocommit=False, check_same_thread=False)
    apply_migrations(conn, MIGRATIONS)
    return conn


def apply_migrations(conn: sqlite3.Connection, scripts: Sequence[str]) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.commit()
    for number, script in enumerate(scripts, start=1):
        if number <= version:
            continue
        try:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
