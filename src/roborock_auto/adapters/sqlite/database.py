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
        days TEXT NOT NULL,
        at_time TEXT NOT NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('clean')),
        action_json TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0
    );
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
