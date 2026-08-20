"""P7: the store contracts — the cleaning log, Room Intervals, Schedule Rules, and
Occurrences — parameterised over persistence adapters.

Runs against the real SQLite adapter — no fake store (docs/adr/0006). Pins what no other
seam structurally can: exact round-tripping of datetimes (timezone-aware, stored as UTC),
enums, durations, Segment lists, and the Rule's Recurrence and Action value objects;
last-cleaned derived as the max started_at per requested Segment, with a whole-house run
counting towards every one of them; device scoping of every row; soft deletion of Rules;
the Recurrence and the Action parsed at the persistence boundary so a malformed row
fails loudly on read;
the Occurrence claim as a conditional update whose atomicity holds under two concurrent
claimers;
and migrations that are idempotent across a re-open and atomic under a mid-script
failure.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from roborock_auto.adapters.sqlite import (
    MIGRATIONS,
    SqliteCleaningLog,
    SqliteOccurrences,
    SqliteRoomIntervals,
    SqliteScheduleRules,
    apply_migrations,
    open_database,
)
from roborock_auto.domain.models import (
    CleanAction,
    CleaningMode,
    CleaningRun,
    CleanSource,
    Occurrence,
    OccurrenceState,
    RoomInterval,
    ScheduleRule,
    Weekday,
    WeeklyGrid,
)

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


@pytest.fixture(params=["sqlite"])
def open_rules(request, tmp_path):
    """Opens a Schedule Rule store over one shared database file; call again to re-open."""
    connections = []

    def _open(device_id: str = "robot-1") -> SqliteScheduleRules:
        conn = open_database(str(tmp_path / "roborock.db"))
        connections.append(conn)
        return SqliteScheduleRules(conn, device_id=device_id)

    yield _open
    for conn in connections:
        conn.close()


def _weekday_mornings(**over) -> dict:
    base = dict(
        name="Weekday mornings",
        recurrence=WeeklyGrid(
            days=frozenset({Weekday.MONDAY, Weekday.WEDNESDAY, Weekday.FRIDAY}),
            at=time(9, 30),
        ),
        action=CleanAction(mode=CleaningMode.VACUUM, segment_ids=(16, 17)),
        enabled=True,
    )
    base.update(over)
    return base


async def test_rules_round_trip_exactly_with_assigned_identities(open_rules):
    store = open_rules()
    weekday = await store.create_rule(**_weekday_mornings())
    weekend = await store.create_rule(
        **_weekday_mornings(
            name="Weekend deep clean",
            recurrence=WeeklyGrid(days=frozenset({Weekday.SATURDAY}), at=time(11, 0)),
            action=CleanAction(mode=CleaningMode.VAC_AND_MOP, segment_ids=None),
            enabled=False,
        )
    )
    assert (weekday, weekend) == (
        ScheduleRule(id=1, **_weekday_mornings()),
        ScheduleRule(
            id=2,
            name="Weekend deep clean",
            recurrence=WeeklyGrid(days=frozenset({Weekday.SATURDAY}), at=time(11, 0)),
            action=CleanAction(mode=CleaningMode.VAC_AND_MOP, segment_ids=None),
            enabled=False,
        ),
    )
    assert await store.list_rules() == [weekday, weekend]


async def test_replacing_a_rule_reports_whether_it_was_there(open_rules):
    store = open_rules()
    created = await store.create_rule(**_weekday_mornings())
    replacement = ScheduleRule(id=created.id, **_weekday_mornings(name="Renamed", enabled=False))
    assert await store.replace_rule(replacement) is True
    assert await store.list_rules() == [replacement]
    assert await store.replace_rule(ScheduleRule(id=99, **_weekday_mornings())) is False


async def test_deleting_a_rule_soft_deletes_it(open_rules):
    store = open_rules()
    created = await store.create_rule(**_weekday_mornings())
    assert await store.delete_rule(created.id) is True
    assert await store.list_rules() == []
    assert await store.delete_rule(created.id) is False
    assert await store.replace_rule(created) is False


async def test_rules_are_device_scoped(open_rules):
    ours, theirs = open_rules("robot-1"), open_rules("robot-2")
    created = await ours.create_rule(**_weekday_mornings())
    assert await theirs.list_rules() == []
    assert await theirs.delete_rule(created.id) is False
    assert await ours.list_rules() == [created]


async def test_reopening_keeps_the_rules(open_rules):
    created = await open_rules().create_rule(**_weekday_mornings())
    assert await open_rules().list_rules() == [created]


async def test_a_deleted_rules_row_survives_for_its_history(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    store = SqliteScheduleRules(conn, device_id="robot-1")
    created = await store.create_rule(**_weekday_mornings())
    await store.delete_rule(created.id)
    assert conn.execute(
        "SELECT id, name, deleted FROM schedule_rules"
    ).fetchall() == [(1, "Weekday mornings", 1)]
    conn.close()


async def test_a_malformed_action_payload_fails_loudly_on_read(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    store = SqliteScheduleRules(conn, device_id="robot-1")
    await store.create_rule(**_weekday_mornings())
    conn.execute("UPDATE schedule_rules SET action_json = '{\"mode\": \"vacuum\"}'")
    conn.commit()
    with pytest.raises(KeyError, match="segment_ids"):
        await store.list_rules()
    conn.close()


async def test_a_malformed_recurrence_payload_fails_loudly_on_read(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    store = SqliteScheduleRules(conn, device_id="robot-1")
    await store.create_rule(**_weekday_mornings())
    conn.execute("UPDATE schedule_rules SET recurrence_json = '{\"days\": [\"monday\"]}'")
    conn.commit()
    with pytest.raises(KeyError, match="at"):
        await store.list_rules()
    conn.close()


def test_an_unknown_action_type_is_blocked_by_the_schema(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    with pytest.raises(sqlite3.IntegrityError, match="action_type"):
        conn.execute(
            "INSERT INTO schedule_rules"
            " (device_id, name, recurrence_type, recurrence_json,"
            " action_type, action_json, enabled)"
            " VALUES ('robot-1', 'r', 'weekly_grid', '{}', 'mop_wash', '{}', 1)"
        )
    conn.close()


def test_an_unknown_recurrence_type_is_blocked_by_the_schema(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    with pytest.raises(sqlite3.IntegrityError, match="recurrence_type"):
        conn.execute(
            "INSERT INTO schedule_rules"
            " (device_id, name, recurrence_type, recurrence_json,"
            " action_type, action_json, enabled)"
            " VALUES ('robot-1', 'r', 'rrule', '{}', 'clean', '{}', 1)"
        )
    conn.close()


@pytest.fixture(params=["sqlite"])
def open_occurrences(request, tmp_path):
    """Opens an Occurrence store over one shared database file; call again to re-open."""
    connections = []

    def _open(device_id: str = "robot-1") -> SqliteOccurrences:
        conn = open_database(str(tmp_path / "roborock.db"))
        connections.append(conn)
        return SqliteOccurrences(conn, device_id=device_id)

    yield _open
    for conn in connections:
        conn.close()


SCHEDULED = datetime(2026, 8, 21, 23, 30, tzinfo=UTC)


def _occurrence(id: int, **over) -> Occurrence:
    base = dict(
        rule_id=1,
        rule_name="Weekday mornings",
        scheduled_for=SCHEDULED,
        deadline=SCHEDULED + timedelta(hours=6),
        state=OccurrenceState.PENDING,
        attempts=0,
        last_attempt_at=None,
        blocked_reason=None,
        fired_at=None,
    )
    base.update(over)
    return Occurrence(id=id, **base)


async def test_occurrences_round_trip_exactly_in_scheduled_order(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    later = SCHEDULED + timedelta(days=3)
    # Each record carries its own Deadline, so two Occurrences can disagree about theirs.
    await store.add_pending(1, scheduled_for=later, deadline=later + timedelta(hours=1))
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.list_occurrences() == [
        _occurrence(2),
        _occurrence(1, scheduled_for=later, deadline=later + timedelta(hours=1)),
    ]


async def test_materialising_the_same_instant_twice_is_a_no_op(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.list_occurrences() == [_occurrence(1)]


async def test_after_cancellation_the_same_instant_materialises_afresh(
    open_rules, open_occurrences
):
    """A cancelled Occurrence keeps its row for history, and an edited Rule that restates
    the same instant gets a fresh PENDING one beside it."""
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.cancel_pending(1)
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.list_occurrences() == [
        _occurrence(1, state=OccurrenceState.CANCELLED),
        _occurrence(2),
    ]


async def test_claim_flips_pending_to_claimed_exactly_once(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.claim(1) is True
    assert await store.claim(1) is False
    assert await store.list_occurrences() == [_occurrence(1, state=OccurrenceState.CLAIMED)]


async def test_claiming_a_missing_or_cancelled_occurrence_is_refused(
    open_rules, open_occurrences
):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.cancel_pending(1)
    assert await store.claim(1) is False
    assert await store.claim(99) is False
    assert await store.list_occurrences() == [_occurrence(1, state=OccurrenceState.CANCELLED)]


async def test_cancel_pending_spares_a_claimed_occurrence_and_other_rules(
    open_rules, open_occurrences
):
    rules = open_rules()
    await rules.create_rule(**_weekday_mornings())
    await rules.create_rule(**_weekday_mornings(name="Weekend mop"))
    store = open_occurrences()
    later = SCHEDULED + timedelta(days=3)
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.add_pending(1, scheduled_for=later, deadline=later + timedelta(hours=6))
    await store.add_pending(2, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.claim(1) is True
    await store.cancel_pending(1)
    assert await store.list_occurrences() == [
        _occurrence(1, state=OccurrenceState.CLAIMED),
        _occurrence(3, rule_id=2, rule_name="Weekend mop"),
        _occurrence(2, scheduled_for=later, deadline=later + timedelta(hours=6),
                    state=OccurrenceState.CANCELLED),
    ]


async def test_a_fired_instant_is_never_rematerialised(open_rules, open_occurrences, tmp_path):
    """Only cancellation frees the (rule, instant) key: a FIRED row keeps holding it, so
    an unconditional materialisation pass can never queue a second firing of the same
    instant."""
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    conn = open_database(str(tmp_path / "roborock.db"))
    conn.execute("UPDATE occurrences SET state = 'fired'")
    conn.commit()
    conn.close()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.list_occurrences() == [_occurrence(1, state=OccurrenceState.FIRED)]


async def test_lifecycle_bookkeeping_round_trips_exactly(open_rules, open_occurrences, tmp_path):
    """Every lifecycle column set with raw SQL, each to a distinct value, so a
    column-to-field mix-up in the read mapping cannot pass regardless of which port
    writer produced the row."""
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    conn = open_database(str(tmp_path / "roborock.db"))
    conn.execute(
        "UPDATE occurrences SET state = 'fired', attempts = 2,"
        " last_attempt_at = '2026-08-21T23:45:00+00:00',"
        " blocked_reason = 'robot busy',"
        " fired_at = '2026-08-22T00:15:00+00:00'"
    )
    conn.commit()
    conn.close()
    assert await store.list_occurrences() == [
        _occurrence(
            1,
            state=OccurrenceState.FIRED,
            attempts=2,
            last_attempt_at=SCHEDULED + timedelta(minutes=15),
            blocked_reason="robot busy",
            fired_at=SCHEDULED + timedelta(minutes=45),
        )
    ]


async def test_a_naive_instant_is_rejected(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.add_pending(
            1,
            scheduled_for=SCHEDULED.replace(tzinfo=None),
            deadline=SCHEDULED + timedelta(hours=6),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.add_pending(
            1,
            scheduled_for=SCHEDULED,
            deadline=(SCHEDULED + timedelta(hours=6)).replace(tzinfo=None),
        )
    assert await store.list_occurrences() == []


async def test_local_instants_are_stored_and_read_back_as_utc(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    sydney = timezone(timedelta(hours=10))
    local = SCHEDULED.astimezone(sydney)
    await store.add_pending(1, scheduled_for=local, deadline=local + timedelta(hours=6))
    (occurrence,) = await store.list_occurrences()
    assert occurrence == _occurrence(1)
    assert occurrence.scheduled_for.utcoffset() == timedelta(0)
    assert occurrence.deadline.utcoffset() == timedelta(0)


async def test_occurrences_are_device_scoped(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    ours, theirs = open_occurrences("robot-1"), open_occurrences("robot-2")
    await ours.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await theirs.list_occurrences() == []
    assert await theirs.claim(1) is False
    assert await ours.list_occurrences() == [_occurrence(1)]


async def test_reopening_keeps_the_occurrences(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    first = open_occurrences()
    await first.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await open_occurrences().list_occurrences() == [_occurrence(1)]


def test_an_unknown_occurrence_state_is_blocked_by_the_schema(tmp_path):
    conn = open_database(str(tmp_path / "roborock.db"))
    with pytest.raises(sqlite3.IntegrityError, match="state"):
        conn.execute(
            "INSERT INTO occurrences (device_id, rule_id, scheduled_for, deadline, state)"
            " VALUES ('robot-1', 1, '2026-08-21T23:30:00+00:00',"
            " '2026-08-22T05:30:00+00:00', 'skipped')"
        )
    conn.close()


async def test_the_claim_is_atomic_under_two_concurrent_claimers(open_rules, tmp_path):
    """Two claimers race on the same PENDING Occurrence from two connections; the
    conditional update lets exactly one of them see the row count of one that is the
    right to fire (docs/adr/0002)."""
    await open_rules().create_rule(**_weekday_mornings())
    path = str(tmp_path / "roborock.db")
    setup = SqliteOccurrences(open_database(path), device_id="robot-1")
    await setup.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))

    claimers = [
        SqliteOccurrences(open_database(path), device_id="robot-1"),
        SqliteOccurrences(open_database(path), device_id="robot-1"),
    ]
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def race(store: SqliteOccurrences) -> None:
        barrier.wait()
        results.append(asyncio.run(store.claim(1)))

    threads = [threading.Thread(target=race, args=(store,)) for store in claimers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    assert await setup.list_occurrences() == [_occurrence(1, state=OccurrenceState.CLAIMED)]


FIRED_AT = SCHEDULED + timedelta(minutes=45)


async def test_mark_blocked_leaves_the_occurrence_pending_with_its_reason(
    open_rules, open_occurrences
):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.mark_blocked(1, "the robot is mid-job")
    assert await store.list_occurrences() == [
        _occurrence(1, blocked_reason="the robot is mid-job")
    ]


async def test_mark_expired_moves_only_a_pending_occurrence(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    later = SCHEDULED + timedelta(days=3)
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.add_pending(1, scheduled_for=later, deadline=later + timedelta(hours=6))
    assert await store.claim(1) is True
    await store.mark_expired(1)  # CLAIMED: commands are in flight, expiry keeps its hands off
    await store.mark_expired(2)
    assert await store.list_occurrences() == [
        _occurrence(1, state=OccurrenceState.CLAIMED),
        _occurrence(
            2,
            scheduled_for=later,
            deadline=later + timedelta(hours=6),
            state=OccurrenceState.EXPIRED,
        ),
    ]


async def test_mark_fired_stamps_the_instant_and_clears_a_stale_blocked_reason(
    open_rules, open_occurrences
):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.mark_blocked(1, "the robot is mid-job")
    assert await store.claim(1) is True
    await store.mark_fired(1, FIRED_AT)
    assert await store.list_occurrences() == [
        _occurrence(1, state=OccurrenceState.FIRED, fired_at=FIRED_AT)
    ]


async def test_mark_fired_requires_the_claim(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await store.mark_fired(1, FIRED_AT)
    assert await store.list_occurrences() == [_occurrence(1)]


async def test_failed_attempts_accumulate_and_the_exhausted_one_is_final(
    open_rules, open_occurrences
):
    await open_rules().create_rule(**_weekday_mornings())
    store = open_occurrences()
    await store.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    assert await store.claim(1) is True
    await store.record_failed_attempt(1, at=FIRED_AT, reason="refused", exhausted=False)
    assert await store.list_occurrences() == [
        _occurrence(1, attempts=1, last_attempt_at=FIRED_AT, blocked_reason="refused")
    ]
    assert await store.claim(1) is True  # back to PENDING: the intent is still live
    await store.record_failed_attempt(
        1, at=FIRED_AT + timedelta(minutes=5), reason="refused again", exhausted=True
    )
    assert await store.list_occurrences() == [
        _occurrence(
            1,
            state=OccurrenceState.FAILED,
            attempts=2,
            last_attempt_at=FIRED_AT + timedelta(minutes=5),
            blocked_reason="refused again",
        )
    ]
    assert await store.claim(1) is False  # FAILED is terminal


async def test_outcome_writes_are_device_scoped(open_rules, open_occurrences):
    await open_rules().create_rule(**_weekday_mornings())
    ours, theirs = open_occurrences("robot-1"), open_occurrences("robot-2")
    await ours.add_pending(1, scheduled_for=SCHEDULED, deadline=SCHEDULED + timedelta(hours=6))
    await theirs.mark_blocked(1, "not yours")
    await theirs.mark_expired(1)
    assert await ours.claim(1) is True
    await theirs.mark_fired(1, FIRED_AT)
    await theirs.record_failed_attempt(1, at=FIRED_AT, reason="not yours", exhausted=True)
    assert await ours.list_occurrences() == [_occurrence(1, state=OccurrenceState.CLAIMED)]
