"""P7: the store contracts — the cleaning log and Room Intervals — parameterised over
persistence adapters.

Runs against the real SQLite adapter — no fake store (docs/adr/0006). Pins what no other
seam structurally can: exact round-tripping of datetimes (timezone-aware, stored as UTC),
enums, durations, and Segment lists; last-cleaned derived as the max started_at per
requested Segment, with a whole-house run counting towards every one of them; device
scoping of every row; and migrations that are idempotent across a re-open and atomic
under a mid-script failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from roborock_auto.adapters.sqlite import (
    MIGRATIONS,
    SqliteCleaningLog,
    SqliteRoomIntervals,
    apply_migrations,
    open_database,
)
from roborock_auto.domain.models import CleaningMode, CleaningRun, CleanSource, RoomInterval

RUN_AT = datetime(2026, 8, 18, 9, 30, tzinfo=UTC)


def _run(**over) -> CleaningRun:
    base = dict(
        source=CleanSource.APP, mode=CleaningMode.VACUUM, segment_ids=(16, 17), started_at=RUN_AT
    )
    base.update(over)
    return CleaningRun(**base)


@pytest.fixture(params=["sqlite"])
def open_log(request, tmp_path):
    """Opens a cleaning log over one shared database file; call again to re-open."""
    connections = []

    def _open(device_id: str = "robot-1") -> SqliteCleaningLog:
        conn = open_database(str(tmp_path / "roborock.db"))
        connections.append(conn)
        return SqliteCleaningLog(conn, device_id=device_id)

    yield _open
    for conn in connections:
        conn.close()


async def test_runs_round_trip_exactly(open_log):
    log = open_log()
    later = _run(
        source=CleanSource.CRON,
        mode=CleaningMode.MOP,
        segment_ids=None,  # whole house
        started_at=RUN_AT + timedelta(hours=2),
    )
    await log.record_run(_run())
    await log.record_run(later)
    assert await log.list_runs() == [_run(), later]


async def test_local_time_is_stored_and_read_back_as_utc(open_log):
    log = open_log()
    sydney = timezone(timedelta(hours=10))
    await log.record_run(_run(started_at=datetime(2026, 8, 18, 19, 30, tzinfo=sydney)))
    (run,) = await log.list_runs()
    assert run == _run(started_at=RUN_AT)
    assert run.started_at.utcoffset() == timedelta(0)


async def test_naive_started_at_is_rejected(open_log):
    log = open_log()
    with pytest.raises(ValueError, match="timezone-aware"):
        await log.record_run(_run(started_at=RUN_AT.replace(tzinfo=None)))
    assert await log.list_runs() == []


async def test_last_cleaned_is_max_started_at_per_requested_segment(open_log):
    log = open_log()
    await log.record_run(_run(segment_ids=(16, 17), started_at=RUN_AT))
    await log.record_run(_run(segment_ids=(17,), started_at=RUN_AT + timedelta(days=1)))
    await log.record_run(_run(segment_ids=(99,), started_at=RUN_AT + timedelta(days=2)))
    assert await log.last_cleaned([16, 17, 18]) == {
        16: RUN_AT,
        17: RUN_AT + timedelta(days=1),
    }


async def test_whole_house_run_counts_towards_every_requested_segment(open_log):
    log = open_log()
    await log.record_run(_run(segment_ids=(16,), started_at=RUN_AT))
    await log.record_run(_run(segment_ids=None, started_at=RUN_AT + timedelta(days=1)))
    await log.record_run(_run(segment_ids=(17,), started_at=RUN_AT + timedelta(days=2)))
    assert await log.last_cleaned([16, 17, 18]) == {
        16: RUN_AT + timedelta(days=1),
        17: RUN_AT + timedelta(days=2),
        18: RUN_AT + timedelta(days=1),
    }


async def test_runs_are_device_scoped(open_log):
    ours, theirs = open_log("robot-1"), open_log("robot-2")
    await ours.record_run(_run())
    assert await theirs.list_runs() == []
    assert await theirs.last_cleaned([16, 17]) == {}
    assert await ours.list_runs() == [_run()]


async def test_reopening_runs_no_migration_and_keeps_data(open_log):
    first = open_log()
    await first.record_run(_run())
    reopened = open_log()
    assert await reopened.list_runs() == [_run()]


def test_reopening_leaves_the_version_cursor_at_the_head(tmp_path):
    path = str(tmp_path / "roborock.db")
    open_database(path).close()
    conn = open_database(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_failed_migration_commits_neither_schema_nor_cursor(tmp_path):
    """The schema change and the cursor advance land in one transaction: a migration that
    dies part-way must leave no half-applied schema and no advanced cursor, so the next
    boot re-runs the (fixed) script instead of failing on its own leftovers forever."""
    conn = open_database(str(tmp_path / "roborock.db"))
    broken = "CREATE TABLE half_done (id INTEGER);\nINSERT INTO missing VALUES (1);"
    with pytest.raises(Exception, match="missing"):
        apply_migrations(conn, (*MIGRATIONS, broken))
    tables = {
        name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "half_done" not in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    # The next boot, with the script fixed, applies cleanly from where it left off.
    apply_migrations(conn, (*MIGRATIONS, "CREATE TABLE half_done (id INTEGER);"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) + 1
    conn.close()


@pytest.fixture(params=["sqlite"])
def open_intervals(request, tmp_path):
    """Opens a Room Interval store over one shared database file; call again to re-open."""
    connections = []

    def _open(device_id: str = "robot-1") -> SqliteRoomIntervals:
        conn = open_database(str(tmp_path / "roborock.db"))
        connections.append(conn)
        return SqliteRoomIntervals(conn, device_id=device_id)

    yield _open
    for conn in connections:
        conn.close()


async def test_intervals_round_trip_exactly_and_list_by_segment(open_intervals):
    store = open_intervals()
    await store.set_interval(RoomInterval(17, timedelta(days=2)))
    await store.set_interval(RoomInterval(16, timedelta(hours=12)))
    assert await store.list_intervals() == [
        RoomInterval(16, timedelta(hours=12)),
        RoomInterval(17, timedelta(days=2)),
    ]


async def test_setting_an_interval_twice_replaces_it(open_intervals):
    store = open_intervals()
    await store.set_interval(RoomInterval(16, timedelta(days=7)))
    await store.set_interval(RoomInterval(16, timedelta(days=3)))
    assert await store.list_intervals() == [RoomInterval(16, timedelta(days=3))]


async def test_removing_reports_whether_an_interval_was_there(open_intervals):
    store = open_intervals()
    await store.set_interval(RoomInterval(16, timedelta(days=2)))
    assert await store.remove_interval(16) is True
    assert await store.remove_interval(16) is False
    assert await store.list_intervals() == []


async def test_intervals_are_device_scoped(open_intervals):
    ours, theirs = open_intervals("robot-1"), open_intervals("robot-2")
    await ours.set_interval(RoomInterval(16, timedelta(days=2)))
    assert await theirs.list_intervals() == []
    assert await theirs.remove_interval(16) is False
    assert await ours.list_intervals() == [RoomInterval(16, timedelta(days=2))]


async def test_reopening_keeps_the_intervals(open_intervals):
    first = open_intervals()
    await first.set_interval(RoomInterval(16, timedelta(hours=36)))
    assert await open_intervals().list_intervals() == [RoomInterval(16, timedelta(hours=36))]
