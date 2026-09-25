"""P8: the scheduler tick — Materialise, select, gate, claim, fire, record — and the
loop's backoff, with an injected wall clock, the fake robot, and a real in-memory
database. Zero real sleeps."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from roborock_auto.adapters.fake.fake_robot import FakeRobot
from roborock_auto.adapters.sqlite import (
    SqliteCleaningLog,
    SqliteOccurrences,
    SqliteScheduleRules,
    open_database,
)
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.scheduler import Scheduler, readiness_block
from roborock_auto.domain.models import (
    CleanAction,
    CleaningMode,
    CleaningRun,
    CleanSource,
    DeviceStatus,
    Occurrence,
    OccurrenceState,
    Weekday,
    WeeklyGrid,
)

START = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)  # Thursday 19:00 in Sydney
SYDNEY = ZoneInfo("Australia/Sydney")
# The Thursday-19:30 rule materialised from START over the 14-day horizon. The horizon
# rolls forward with the clock, so a tick after FIRST also materialises the fortnight row.
FIRST = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
NEXT_WEEK = FIRST + timedelta(days=7)
FORTNIGHT = FIRST + timedelta(days=14)

BUSY = DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True)
DOCKED_IDLE = DeviceStatus("charging", 100, in_cleaning=False, docked=True)


class Clock:
    """One clock for both time axes: the scheduler's wall datetime and the fake robot's
    monotonic seconds."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return (self.now - START).total_seconds()


class Harness:
    def __init__(self, robot_cls=FakeRobot):
        self.clock = Clock()
        conn = open_database(":memory:")
        self.robot = robot_cls(clock=self.clock.monotonic)
        self.rules = SqliteScheduleRules(conn, device_id="fake")
        self.occurrences = SqliteOccurrences(conn, device_id="fake")
        self.cleaning_log = SqliteCleaningLog(conn, device_id="fake")
        self.run_slot = RunSlot()
        self.scheduler = self.new_scheduler()

    def new_scheduler(self) -> Scheduler:
        """A fresh Scheduler over the same durable store — a process restart."""
        return Scheduler(
            self.robot,
            rules=self.rules,
            occurrences=self.occurrences,
            cleaning_log=self.cleaning_log,
            tz=SYDNEY,
            now=self.clock,
            run_slot=self.run_slot,
        )

    async def rule(self, *, name="Evening vacuum", at=time(19, 30), action=None):
        return await self.rules.create_rule(
            name=name,
            recurrence=WeeklyGrid(days=frozenset({Weekday.THURSDAY}), at=at),
            action=action or CleanAction(mode=CleaningMode.VACUUM, segment_ids=(16, 17)),
            enabled=True,
        )


def _occ(
    id,
    *,
    scheduled_for=FIRST,
    rule_id=1,
    rule_name="Evening vacuum",
    state=OccurrenceState.PENDING,
    attempts=0,
    last_attempt_at=None,
    blocked_reason=None,
    fired_at=None,
) -> Occurrence:
    return Occurrence(
        id=id,
        rule_id=rule_id,
        rule_name=rule_name,
        scheduled_for=scheduled_for,
        deadline=scheduled_for + timedelta(hours=6),
        state=state,
        attempts=attempts,
        last_attempt_at=last_attempt_at,
        blocked_reason=blocked_reason,
        fired_at=fired_at,
    )


async def test_a_tick_before_the_instant_materialises_and_leaves_the_robot_alone():
    h = Harness()
    await h.rule()
    await h.scheduler.tick()
    assert h.robot.commands == []
    assert await h.occurrences.list_occurrences() == [
        _occ(1),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]


async def test_a_due_occurrence_fires_the_shared_clean_and_is_recorded():
    h = Harness()
    await h.rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    assert h.robot.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17)),
    ]
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]
    assert await h.cleaning_log.list_runs() == [
        CleaningRun(
            source=CleanSource.SCHEDULER,
            mode=CleaningMode.VACUUM,
            segment_ids=(16, 17),
            started_at=FIRST,
        )
    ]


def test_the_readiness_gate_reuses_ready_to_start_and_names_the_leg_that_failed():
    assert readiness_block(DOCKED_IDLE, run_in_flight=False) is None
    assert (
        readiness_block(DOCKED_IDLE, run_in_flight=True) == "a two-stage clean is in flight"
    )
    assert readiness_block(BUSY, run_in_flight=False) == "the robot is mid-job"
    errored = DeviceStatus("error", 50, in_cleaning=False, docked=True, error="brush stuck")
    assert (
        readiness_block(errored, run_in_flight=False)
        == "the robot reports an error: brush stuck"
    )
    stranded = DeviceStatus("idle", 80, in_cleaning=False, docked=False)
    assert readiness_block(stranded, run_in_flight=False) == "the robot is not docked"


async def test_a_busy_robot_leaves_the_occurrence_pending_without_an_attempt_then_fires():
    h = Harness()
    h.robot.set_timeline([(0, BUSY), (3600, DOCKED_IDLE)])  # busy until 10:00 UTC
    await h.rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    assert h.robot.commands == []
    assert await h.occurrences.list_occurrences() == [
        _occ(1, blocked_reason="the robot is mid-job"),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]
    h.clock.now = FIRST + timedelta(minutes=45)  # the robot is home again
    await h.scheduler.tick()
    assert h.robot.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17)),
    ]
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST + timedelta(minutes=45)),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(3, scheduled_for=FORTNIGHT),
    ]


async def test_a_two_stage_clean_in_flight_blocks_firing_without_an_attempt():
    h = Harness()
    await h.rule()
    assert h.run_slot.acquire() is True
    h.clock.now = FIRST
    await h.scheduler.tick()
    assert h.robot.commands == []
    assert await h.occurrences.list_occurrences() == [
        _occ(1, blocked_reason="a two-stage clean is in flight"),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]
    h.run_slot.release()
    await h.scheduler.tick()
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]


async def test_an_occurrence_past_its_deadline_expires_and_never_fires():
    h = Harness()
    await h.rule()
    await h.scheduler.tick()  # materialised while still upcoming
    h.clock.now = FIRST + timedelta(hours=6)  # the six-hour default Deadline has passed
    await h.scheduler.tick()
    assert h.robot.commands == []
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.EXPIRED),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(3, scheduled_for=FORTNIGHT),
    ]


class RefusingRobot(FakeRobot):
    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        raise RuntimeError("the robot refused the clean")


async def test_a_genuine_refusal_is_retried_three_times_five_minutes_apart_then_failed():
    h = Harness(robot_cls=RefusingRobot)
    await h.rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    refused = dict(blocked_reason="the robot refused the clean")
    assert await h.occurrences.list_occurrences() == [
        _occ(1, attempts=1, last_attempt_at=FIRST, **refused),
        _occ(2, scheduled_for=NEXT_WEEK),
    ]

    h.clock.now = FIRST + timedelta(minutes=4)  # inside the five-minute spacing
    await h.scheduler.tick()
    assert await h.occurrences.list_occurrences() == [
        _occ(1, attempts=1, last_attempt_at=FIRST, **refused),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(3, scheduled_for=FORTNIGHT),
    ]

    h.clock.now = FIRST + timedelta(minutes=5)
    await h.scheduler.tick()
    h.clock.now = FIRST + timedelta(minutes=10)
    await h.scheduler.tick()
    assert await h.occurrences.list_occurrences() == [
        _occ(
            1,
            state=OccurrenceState.FAILED,
            attempts=3,
            last_attempt_at=FIRST + timedelta(minutes=10),
            **refused,
        ),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(3, scheduled_for=FORTNIGHT),
    ]

    h.clock.now = FIRST + timedelta(minutes=20)  # FAILED is terminal: no fourth attempt
    await h.scheduler.tick()
    assert h.robot.commands == [("set_cleaning_mode", CleaningMode.VACUUM)] * 3
    assert await h.cleaning_log.list_runs() == []


async def test_two_occurrences_due_together_fire_in_order_across_ticks():
    h = Harness()
    await h.rule(name="Evening vacuum")
    await h.rule(
        name="Evening mop", action=CleanAction(mode=CleaningMode.MOP, segment_ids=None)
    )
    h.clock.now = FIRST
    await h.scheduler.tick()
    # The first firing makes the robot busy, so the second waits — neither is dropped.
    mop = dict(rule_id=2, rule_name="Evening mop")
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST),
        _occ(3, blocked_reason="the robot is mid-job", **mop),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(4, scheduled_for=NEXT_WEEK, **mop),
    ]
    h.clock.now = FIRST + timedelta(minutes=5)  # the fake's job has finished
    await h.scheduler.tick()
    assert h.robot.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("clean_rooms", (16, 17)),
        ("set_cleaning_mode", CleaningMode.MOP),
        ("start",),
    ]
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST),
        _occ(3, state=OccurrenceState.FIRED, fired_at=FIRST + timedelta(minutes=5), **mop),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(4, scheduled_for=NEXT_WEEK, **mop),
        _occ(5, scheduled_for=FORTNIGHT),
        _occ(6, scheduled_for=FORTNIGHT, **mop),
    ]


async def test_a_firing_missed_by_a_restart_inside_the_deadline_still_happens():
    h = Harness()
    await h.rule()
    await h.scheduler.tick()  # the Occurrence is durable
    restarted = h.new_scheduler()
    h.clock.now = FIRST + timedelta(hours=1)  # the restart outlasted the instant
    await restarted.tick()
    assert await h.occurrences.list_occurrences() == [
        _occ(1, state=OccurrenceState.FIRED, fired_at=FIRST + timedelta(hours=1)),
        _occ(2, scheduled_for=NEXT_WEEK),
        _occ(3, scheduled_for=FORTNIGHT),
    ]


class ExplodingRules:
    def __init__(self) -> None:
        self.fail = True

    async def list_rules(self):
        if self.fail:
            raise RuntimeError("database is on fire")
        return []


class SleepRecorder:
    def __init__(self, limit: int) -> None:
        self.delays: list[float] = []
        self._limit = limit

    async def __call__(self, sec: float) -> None:
        self.delays.append(sec)
        if len(self.delays) >= self._limit:
            raise asyncio.CancelledError


async def test_the_loop_survives_tick_exceptions_and_backs_off_capped_at_five_minutes():
    h = Harness()
    sleeps = SleepRecorder(limit=6)
    broken = Scheduler(
        h.robot,
        rules=ExplodingRules(),
        occurrences=h.occurrences,
        cleaning_log=h.cleaning_log,
        tz=SYDNEY,
        now=h.clock,
        sleep=sleeps,
    )
    with pytest.raises(asyncio.CancelledError):
        await broken.run()
    assert sleeps.delays == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert broken.consecutive_errors == 5
    assert broken.last_tick_at == START


async def test_the_backoff_stays_capped_after_a_multi_day_outage():
    h = Harness()
    broken = Scheduler(
        h.robot,
        rules=ExplodingRules(),
        occurrences=h.occurrences,
        cleaning_log=h.cleaning_log,
        tz=SYDNEY,
        now=h.clock,
    )
    broken.consecutive_errors = 1030  # past where 2**n overflows float conversion
    await broken.tick_safely()
    assert (broken.consecutive_errors, broken.backoff_sec()) == (1031, 300.0)


async def test_a_healthy_tick_resets_the_backoff():
    h = Harness()
    rules = ExplodingRules()
    flaky = Scheduler(
        h.robot,
        rules=rules,
        occurrences=h.occurrences,
        cleaning_log=h.cleaning_log,
        tz=SYDNEY,
        now=h.clock,
    )
    await flaky.tick_safely()
    await flaky.tick_safely()
    assert (flaky.consecutive_errors, flaky.backoff_sec()) == (2, 120.0)
    rules.fail = False
    await flaky.tick_safely()
    assert (flaky.consecutive_errors, flaky.backoff_sec()) == (0, 30.0)
