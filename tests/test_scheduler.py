"""P8: the scheduler tick — Materialise, select, gate, claim, fire, record — and the
loop's backoff, with an injected wall clock, the fake robot, and a real in-memory
database. Zero real sleeps."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from roborock_auto.adapters.fake.fake_robot import FakeRobot
from roborock_auto.adapters.sqlite import (
    SqliteCleaningLog,
    SqliteOccurrences,
    SqliteScheduleRules,
    SqliteSequences,
    open_database,
)
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.scheduler import Scheduler, readiness_block
from roborock_auto.application.sequence_launch import SequenceLauncher, step_body
from roborock_auto.config import Config
from roborock_auto.domain.models import (
    CarpetMode,
    CleanAction,
    CleaningMode,
    CleaningRun,
    CleanSource,
    DeviceStatus,
    FanPower,
    MopMode,
    Occurrence,
    OccurrenceState,
    SequenceAction,
    Step,
    WaterMode,
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
DOCKED_IDLE = DeviceStatus("charging", 100, in_cleaning=False, docked=True, cleaning_mode="vacuum")


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
        ("set_fan_power", FanPower.BALANCED),  # the fake's Setup, kept across the switch
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
        readiness_block(DOCKED_IDLE, run_in_flight=True) == "another run is in flight"
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
        _occ(1, blocked_reason="another run is in flight"),
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
    assert h.robot.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM), ("set_fan_power", FanPower.BALANCED),
    ] * 3
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
    # The vacuum switched water off, so the mop has only Setup's route left to keep.
    assert h.robot.commands == [
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_fan_power", FanPower.BALANCED),
        ("clean_rooms", (16, 17)),
        ("set_cleaning_mode", CleaningMode.MOP),
        ("set_mop_mode", MopMode.STANDARD),
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


# --- Sequence Actions: the scheduler starts the runner in the background ---

CONFIG = Config(
    gateway="fake", email="", duid="", carpet_rise_value=0, carpet_avoid_value=1,
    poll_interval_sec=5.0, app_port=8000, session_path="user_data.json",
    database_path=":memory:", timezone="Australia/Sydney",
)
BATHROOMS_THEN_HOUSE = (
    Step(mode=CleaningMode.VACUUM, carpet=CarpetMode.RISE, segment_ids=(16, 17)),
    Step(mode=CleaningMode.VAC_AND_MOP, carpet=CarpetMode.AVOID, segment_ids=None),
)
BATHROOMS_THEN_HOUSE_COMMANDS = [
    ("set_carpet_mode", CarpetMode.RISE),
    ("set_cleaning_mode", CleaningMode.VACUUM),
    ("set_fan_power", FanPower.BALANCED),
    ("clean_rooms", (16, 17)),
    ("set_carpet_mode", CarpetMode.AVOID),
    ("set_cleaning_mode", CleaningMode.VAC_AND_MOP),
    ("set_fan_power", FanPower.BALANCED),
    ("set_water_mode", WaterMode.STANDARD),
    ("set_mop_mode", MopMode.STANDARD),
    ("start",),
]


class SequenceHarness(Harness):
    """The Harness plus a Sequence store and a launcher whose runner sleeps on the same
    fake clock, so a whole Sequence runs in milliseconds."""

    def __init__(
        self,
        robot_cls=FakeRobot,
        occurrences_cls=SqliteOccurrences,
        cleaning_log_cls=SqliteCleaningLog,
    ):
        self.clock = Clock()
        conn = open_database(":memory:")
        self.robot = robot_cls(clock=self.clock.monotonic)
        self.rules = SqliteScheduleRules(conn, device_id="fake")
        self.occurrences = occurrences_cls(conn, device_id="fake")
        self.cleaning_log = cleaning_log_cls(conn, device_id="fake")
        self.sequences = SqliteSequences(conn, device_id="fake")
        self.run_slot = RunSlot()
        self.launcher = SequenceLauncher(
            self.robot,
            CONFIG,
            slot=self.run_slot,
            cleaning_log=self.cleaning_log,
            now=self.clock,
            clock=self.clock.monotonic,
            sleep=self.sleep,
            log=lambda _line: None,
        )
        self.scheduler = Scheduler(
            self.robot,
            rules=self.rules,
            occurrences=self.occurrences,
            cleaning_log=self.cleaning_log,
            tz=SYDNEY,
            now=self.clock,
            run_slot=self.run_slot,
            sequences=self.sequences,
            launcher=self.launcher,
        )

    async def sleep(self, seconds: float) -> None:
        self.clock.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    async def sequence_rule(self, *, name="Saturday deep clean", steps=BATHROOMS_THEN_HOUSE):
        sequence = await self.sequences.create_sequence("Bathrooms then house", steps)
        return await self.rule(name=name, action=SequenceAction(sequence_id=sequence.id))


async def test_a_due_sequence_rule_starts_the_run_in_the_background_and_returns():
    h = SequenceHarness()
    await h.sequence_rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    # The tick returned without waiting on the run: nothing has reached the robot yet,
    # the slot is held, and the Occurrence is claimed but not yet fired.
    assert h.robot.commands == []
    assert h.run_slot.running is True
    assert h.run_slot.task is not None and not h.run_slot.task.done()
    assert await h.occurrences.list_occurrences() == [
        _occ(1, rule_name="Saturday deep clean", state=OccurrenceState.CLAIMED),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]


async def test_a_scheduled_sequence_runs_every_step_and_is_fired_on_its_first_departure():
    h = SequenceHarness()
    await h.sequence_rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    await h.run_slot.task
    first_departure = FIRST + timedelta(seconds=10)
    second_departure = FIRST + timedelta(seconds=45)
    assert h.robot.commands == BATHROOMS_THEN_HOUSE_COMMANDS
    assert await h.cleaning_log.list_runs() == [
        CleaningRun(CleanSource.SCHEDULER, CleaningMode.VACUUM, (16, 17), first_departure),
        CleaningRun(CleanSource.SCHEDULER, CleaningMode.VAC_AND_MOP, None, second_departure),
    ]
    assert await h.occurrences.list_occurrences() == [
        _occ(
            1, rule_name="Saturday deep clean", state=OccurrenceState.FIRED,
            fired_at=first_departure,
        ),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]
    assert h.run_slot.running is False
    assert h.run_slot.progress == {
        "stage": "done",
        "message": "Sequence complete",
        "error": None,
        "sequence_id": 1,
        "sequence_name": "Bathrooms then house",
        "source": "scheduler",
        "step": 1,
        "step_count": 2,
        "steps": [step_body(step) for step in BATHROOMS_THEN_HOUSE],
        "phase": "done",
    }


async def test_a_cancel_before_the_first_departure_fails_the_occurrence_for_good():
    h = SequenceHarness()
    await h.sequence_rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    h.run_slot.cancel.set()
    await h.run_slot.task
    assert h.robot.commands == [
        ("set_carpet_mode", CarpetMode.RISE),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_fan_power", FanPower.BALANCED),
        ("clean_rooms", (16, 17)),
        ("set_carpet_mode", CarpetMode.AVOID),
        ("dock",),
    ]
    cancelled = _occ(
        1, rule_name="Saturday deep clean", state=OccurrenceState.FAILED, attempts=1,
        last_attempt_at=FIRST, blocked_reason="cancelled",
    )
    assert await h.occurrences.list_occurrences() == [
        cancelled,
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]
    h.clock.now = FIRST + timedelta(minutes=10)  # well past the retry spacing
    await h.scheduler.tick()
    assert h.run_slot.running is False
    assert len(h.robot.commands) == 6
    assert await h.cleaning_log.list_runs() == []
    assert (await h.occurrences.list_occurrences())[0] == cancelled


class DropsEveryCleanRobot(FakeRobot):
    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        self.commands.append(("clean_rooms", tuple(segment_ids)))


async def test_a_failure_before_the_first_departure_is_a_failed_attempt_to_retry():
    h = SequenceHarness(robot_cls=DropsEveryCleanRobot)
    await h.sequence_rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    await h.run_slot.task
    failed_at = FIRST + timedelta(minutes=3)  # three dropped triggers, 60 s grace each
    assert await h.occurrences.list_occurrences() == [
        _occ(
            1, rule_name="Saturday deep clean", attempts=1, last_attempt_at=failed_at,
            blocked_reason="job never started (trigger did not take)",
        ),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]
    assert await h.cleaning_log.list_runs() == []
    assert h.run_slot.running is False


async def test_a_second_due_occurrence_in_the_same_tick_is_blocked_while_the_slot_is_held():
    h = SequenceHarness()
    await h.sequence_rule()
    await h.rule(name="Evening mop", action=CleanAction(mode=CleaningMode.MOP, segment_ids=None))
    h.clock.now = FIRST
    await h.scheduler.tick()
    mop = dict(rule_id=2, rule_name="Evening mop")
    assert h.robot.commands == []
    assert await h.occurrences.list_occurrences() == [
        _occ(1, rule_name="Saturday deep clean", state=OccurrenceState.CLAIMED),
        _occ(3, blocked_reason="another run is in flight", **mop),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
        _occ(4, scheduled_for=NEXT_WEEK, **mop),
    ]


class SlotWitnessRobot(FakeRobot):
    """Records whether the RunSlot is held at the readiness check and at the start."""

    slot: RunSlot

    def __init__(self, clock):
        super().__init__(clock=clock)
        self.slot_held: list[tuple[str, bool]] = []

    async def get_status(self) -> DeviceStatus:
        self.slot_held.append(("get_status", self.slot.running))
        return await super().get_status()

    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        self.slot_held.append(("clean_rooms", self.slot.running))
        await super().clean_rooms(segment_ids, repeat)


async def test_a_plain_clean_holds_the_run_slot_from_its_readiness_check_to_its_start():
    h = Harness(robot_cls=SlotWitnessRobot)
    h.robot.slot = h.run_slot
    await h.rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    assert h.robot.slot_held == [
        ("get_status", True), ("get_status", True), ("get_status", True), ("clean_rooms", True),
    ]
    assert h.run_slot.running is False


class MarkFiredFails(SqliteOccurrences):
    async def mark_fired(self, occurrence_id, fired_at) -> None:
        raise RuntimeError("database is locked")


class RefusesWholeHouseRobot(FakeRobot):
    async def start(self) -> None:
        raise RuntimeError("the robot refused the clean")


async def test_a_failed_fired_mark_is_logged_and_step_one_never_fires_again(caplog):
    h = SequenceHarness(robot_cls=RefusesWholeHouseRobot, occurrences_cls=MarkFiredFails)
    await h.sequence_rule()
    h.clock.now = FIRST
    with caplog.at_level(logging.ERROR, logger="roborock_auto.scheduler"):
        await h.scheduler.tick()
        await h.run_slot.task
    assert [record.getMessage() for record in caplog.records] == [
        "occurrence=1 rule=1 could not be marked fired; it stays claimed and is not re-fired"
    ]
    # Step 2 failed after Step 1 departed: no failed attempt, so no retry of Step 1.
    assert await h.occurrences.list_occurrences() == [
        _occ(1, rule_name="Saturday deep clean", state=OccurrenceState.CLAIMED),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]
    commands = list(h.robot.commands)
    h.clock.now = FIRST + timedelta(minutes=30)
    await h.scheduler.tick()
    assert h.robot.commands == commands
    assert h.run_slot.progress == {
        "stage": "error",
        "message": "the robot refused the clean",
        "error": "the robot refused the clean",
        "sequence_id": 1,
        "sequence_name": "Bathrooms then house",
        "source": "scheduler",
        "step": 1,
        "step_count": 2,
        "steps": [step_body(step) for step in BATHROOMS_THEN_HOUSE],
        "phase": "departing",
    }


class StatusFailsRobot(FakeRobot):
    async def get_status(self) -> DeviceStatus:
        raise RuntimeError("lost contact")


async def test_a_readiness_check_that_raises_still_releases_the_run_slot():
    h = Harness(robot_cls=StatusFailsRobot)
    await h.rule()
    h.clock.now = FIRST
    await h.scheduler.tick_safely()
    assert h.scheduler.consecutive_errors == 1
    assert h.run_slot.running is False


async def test_a_rule_whose_sequence_is_gone_records_a_failed_attempt():
    h = SequenceHarness()
    await h.rule(name="Orphan", action=SequenceAction(sequence_id=99))
    h.clock.now = FIRST
    await h.scheduler.tick()
    assert h.robot.commands == []
    assert h.run_slot.running is False
    assert await h.occurrences.list_occurrences() == [
        _occ(
            1, rule_name="Orphan", attempts=1, last_attempt_at=FIRST,
            blocked_reason="no Sequence 99",
        ),
        _occ(2, rule_name="Orphan", scheduled_for=NEXT_WEEK),
    ]


async def test_starting_the_scheduler_fails_an_occurrence_a_restart_left_claimed():
    h = Harness()
    await h.rule()
    await h.scheduler.tick()
    assert await h.occurrences.claim(1) is True  # the process died before it resolved
    restarted = h.new_scheduler()
    restarted._sleep = SleepRecorder(limit=1)
    with pytest.raises(asyncio.CancelledError):
        await restarted.run()
    interrupted = _occ(
        1, state=OccurrenceState.FAILED, attempts=1, last_attempt_at=START,
        blocked_reason="interrupted by a restart",
    )
    assert await h.occurrences.list_occurrences() == [
        interrupted,
        _occ(2, scheduled_for=NEXT_WEEK),
    ]
    h.clock.now = FIRST
    await restarted.tick()
    assert h.robot.commands == []
    assert (await h.occurrences.list_occurrences())[0] == interrupted


class RecordRunFails(SqliteCleaningLog):
    async def record_run(self, run) -> None:
        raise RuntimeError("disk full")


async def test_a_failed_cleaning_run_write_after_departure_still_fires_and_never_retries():
    h = SequenceHarness(cleaning_log_cls=RecordRunFails)
    await h.sequence_rule()
    h.clock.now = FIRST
    await h.scheduler.tick()
    await h.run_slot.task
    step_one_then_recovery = [
        ("set_carpet_mode", CarpetMode.RISE),
        ("set_cleaning_mode", CleaningMode.VACUUM),
        ("set_fan_power", FanPower.BALANCED),
        ("clean_rooms", (16, 17)),
        ("set_carpet_mode", CarpetMode.AVOID),
        ("dock",),
    ]
    assert h.robot.commands == step_one_then_recovery
    assert await h.occurrences.list_occurrences() == [
        _occ(
            1, rule_name="Saturday deep clean", state=OccurrenceState.FIRED,
            fired_at=FIRST + timedelta(seconds=10),
        ),
        _occ(2, rule_name="Saturday deep clean", scheduled_for=NEXT_WEEK),
    ]
    h.clock.now = FIRST + timedelta(minutes=30)
    await h.scheduler.tick()
    assert h.robot.commands == step_one_then_recovery
