"""Up next: queued Cleans start once the robot is home, in order, and only after a success."""

from __future__ import annotations

import asyncio

from roborock_auto.adapters.fake import FakeRobot
from roborock_auto.adapters.sqlite import SqliteCleaningLog, open_database
from roborock_auto.application.clean_queue import (
    LAST_RUN_CANCELLED,
    LAST_RUN_FAILED,
    STOPPED_BY_USER,
    CleanQueue,
)
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.sequence_launch import SequenceLauncher
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, CleaningMode, DeviceStatus, QueuedClean, Step

CONFIG = Config(
    gateway="fake",
    email="",
    duid="",
    carpet_rise_value=0,
    carpet_avoid_value=1,
    poll_interval_sec=5.0,
    app_port=8000,
    session_path="user_data.json",
    database_path=":memory:",
    timezone="Australia/Sydney",
)
KITCHEN_TWICE = (Step(CleaningMode.VACUUM, CarpetMode.AVOID, (17,), repeat=2),)
HALL_MOP = (Step(CleaningMode.MOP, CarpetMode.AVOID, (19,)),)
KITCHEN_TWICE_COMMANDS = [
    ("set_carpet_mode", CarpetMode.AVOID),
    ("set_cleaning_mode", CleaningMode.VACUUM),
    ("clean_rooms", (17,), 2),
]
HALL_MOP_COMMANDS = [
    ("set_carpet_mode", CarpetMode.AVOID),
    ("set_cleaning_mode", CleaningMode.MOP),
    ("clean_rooms", (19,)),
]


class Harness:
    def __init__(self, robot_cls=FakeRobot):
        self.now = 0.0
        self.robot = robot_cls(clock=lambda: self.now)
        self.slot = RunSlot()
        launcher = SequenceLauncher(
            self.robot,
            CONFIG,
            slot=self.slot,
            cleaning_log=SqliteCleaningLog(open_database(":memory:"), device_id="fake"),
            clock=lambda: self.now,
            sleep=self.sleep,
            log=lambda _line: None,
        )
        self.queue = CleanQueue(
            self.robot,
            slot=self.slot,
            launcher=launcher,
            interval=5.0,
            sleep=self.sleep,
            log=lambda _line: None,
        )

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    async def drain(self) -> None:
        """Run the loop until the queue's task has ended (drained or held)."""
        for _ in range(2000):
            await asyncio.sleep(0)
            if self.queue.task is None or self.queue.task.done():
                return
        raise AssertionError("the queue never settled")

    async def spin(self, turns: int = 4) -> None:
        """A few polls' worth of loop turns: enough to see the queue waiting, too few
        for the fake's 30 s job to end."""
        for _ in range(turns):
            await asyncio.sleep(0)


def _state(queue: CleanQueue) -> dict:
    return {
        "held": queue.held,
        "hold_reason": queue.hold_reason,
        "waiting_for": queue.waiting_for,
        "entries": list(queue.entries),
    }


def _idle(*entries: QueuedClean) -> dict:
    return {"held": False, "hold_reason": None, "waiting_for": None, "entries": list(entries)}


async def test_a_clean_queued_while_the_robot_is_out_starts_once_it_is_home():
    h = Harness()
    await h.robot.start()  # a plain Clean already under way
    h.queue.add("Kitchen · Vacuum · 2 passes", KITCHEN_TWICE)
    await h.spin()
    assert _state(h.queue) == {
        "held": False,
        "hold_reason": None,
        "waiting_for": "the robot is mid-job",
        "entries": [QueuedClean(1, "Kitchen · Vacuum · 2 passes", KITCHEN_TWICE)],
    }
    assert h.robot.commands == [("start",)]
    await h.drain()
    assert _state(h.queue) == _idle()
    assert h.robot.commands == [("start",), *KITCHEN_TWICE_COMMANDS]
    assert h.slot.progress["stage"] == "done"
    assert h.slot.progress["sequence_name"] == "Kitchen · Vacuum · 2 passes"
    assert h.slot.progress["sequence_id"] is None


async def test_entries_run_in_the_order_they_were_added():
    h = Harness()
    await h.robot.start()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.add("Hall", HALL_MOP, sequence_id=4)
    await h.drain()
    assert _state(h.queue) == _idle()
    assert h.robot.commands == [("start",), *KITCHEN_TWICE_COMMANDS, *HALL_MOP_COMMANDS]
    assert (h.slot.progress["sequence_name"], h.slot.progress["sequence_id"]) == ("Hall", 4)


async def test_the_queue_waits_while_another_run_holds_the_slot():
    h = Harness()
    assert h.slot.acquire()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    await h.spin()
    assert h.queue.waiting_for == "another run is in flight"
    assert h.robot.commands == []
    h.slot.release()
    await h.drain()
    assert h.robot.commands == KITCHEN_TWICE_COMMANDS


async def test_a_cancelled_run_holds_the_queue_with_the_rest_still_queued():
    h = Harness()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.add("Hall", HALL_MOP)
    while ("clean_rooms", (17,), 2) not in h.robot.commands:
        await asyncio.sleep(0)
    h.slot.cancel.set()
    await h.drain()
    assert _state(h.queue) == {
        "held": True,
        "hold_reason": LAST_RUN_CANCELLED,
        "waiting_for": None,
        "entries": [QueuedClean(2, "Hall", HALL_MOP)],
    }
    assert h.robot.commands == [
        *KITCHEN_TWICE_COMMANDS,
        ("set_carpet_mode", CarpetMode.AVOID),
        ("dock",),
    ]
    h.queue.resume()
    await h.drain()
    assert _state(h.queue) == _idle()
    assert h.robot.commands[-3:] == HALL_MOP_COMMANDS


class CarpetRefusesRobot(FakeRobot):
    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        raise RuntimeError("carpet setting refused")


async def test_a_failed_run_holds_the_queue():
    h = Harness(CarpetRefusesRobot)
    h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.add("Hall", HALL_MOP)
    await h.drain()
    assert _state(h.queue) == {
        "held": True,
        "hold_reason": LAST_RUN_FAILED,
        "waiting_for": None,
        "entries": [QueuedClean(2, "Hall", HALL_MOP)],
    }
    assert h.slot.progress["stage"] == "error"
    assert h.slot.running is False


async def test_a_hold_while_waiting_sends_nothing_until_resumed():
    h = Harness()
    await h.robot.start()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    await h.spin()
    h.queue.hold(STOPPED_BY_USER)
    await h.drain()
    assert _state(h.queue) == {
        "held": True,
        "hold_reason": STOPPED_BY_USER,
        "waiting_for": None,
        "entries": [QueuedClean(1, "Kitchen", KITCHEN_TWICE)],
    }
    # The robot finishes its own job; the queue still does not start anything.
    h.now += 60
    await h.spin()
    assert h.robot.commands == [("start",)]
    h.queue.resume()
    await h.drain()
    assert h.robot.commands == [("start",), *KITCHEN_TWICE_COMMANDS]


async def test_the_first_hold_reason_wins():
    h = Harness()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.add("Hall", HALL_MOP)
    while ("clean_rooms", (17,), 2) not in h.robot.commands:
        await asyncio.sleep(0)
    # The user's Stop reaches the queue before the run reports the cancel it caused.
    h.queue.hold(STOPPED_BY_USER)
    h.slot.cancel.set()
    await h.drain()
    assert (h.queue.held, h.queue.hold_reason) == (True, STOPPED_BY_USER)


async def test_a_hold_with_nothing_queued_is_ignored():
    h = Harness()
    h.queue.hold(STOPPED_BY_USER)
    assert _state(h.queue) == _idle()


async def test_remove_move_and_clear():
    h = Harness()
    assert h.slot.acquire()  # keep the robot's turn from coming while the list is edited
    kitchen = h.queue.add("Kitchen", KITCHEN_TWICE)
    hall = h.queue.add("Hall", HALL_MOP)
    study = h.queue.add("Study", HALL_MOP)
    assert h.queue.move(study.id, 0) is True
    assert h.queue.entries == [study, kitchen, hall]
    assert h.queue.move(study.id, 99) is True
    assert h.queue.entries == [kitchen, hall, study]
    assert h.queue.move(42, 0) is False
    assert h.queue.remove(hall.id) is True
    assert h.queue.remove(hall.id) is False
    assert h.queue.entries == [kitchen, study]
    h.queue.hold(STOPPED_BY_USER)
    h.queue.clear()
    assert _state(h.queue) == _idle()
    h.slot.release()
    await h.drain()
    assert h.robot.commands == []


async def test_removing_the_last_entry_lifts_a_hold():
    h = Harness()
    assert h.slot.acquire()
    kitchen = h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.hold(STOPPED_BY_USER)
    h.queue.remove(kitchen.id)
    assert _state(h.queue) == _idle()


class HoldDuringReadRobot(FakeRobot):
    """Calls back on every status read, so a test can act in the gap between the read
    that confirms readiness and the launch that follows it."""

    on_read = staticmethod(lambda: None)

    async def get_status(self) -> DeviceStatus:
        status = await super().get_status()
        self.on_read()
        return status


async def test_a_hold_during_the_confirming_status_read_still_sends_nothing():
    h = Harness(HoldDuringReadRobot)
    reads = 0

    def on_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            h.queue.hold(STOPPED_BY_USER)

    h.robot.on_read = on_read
    h.queue.add("Kitchen", KITCHEN_TWICE)
    await h.drain()
    assert h.robot.commands == []
    assert (h.queue.held, h.queue.hold_reason, h.slot.running) == (True, STOPPED_BY_USER, False)


async def test_a_clear_during_the_confirming_status_read_leaves_the_slot_free():
    h = Harness(HoldDuringReadRobot)
    reads = 0

    def on_read():
        nonlocal reads
        reads += 1
        if reads == 2:
            h.queue.clear()

    h.robot.on_read = on_read
    h.queue.add("Kitchen", KITCHEN_TWICE)
    await h.drain()
    assert h.robot.commands == []
    assert h.slot.running is False
    assert h.queue.task.exception() is None
    assert _state(h.queue) == _idle()


async def test_readiness_must_hold_across_two_consecutive_polls():
    h = Harness()
    docked = DeviceStatus("charging", 100, in_cleaning=False, docked=True, cleaning_mode="vacuum")
    busy = DeviceStatus("cleaning", 90, in_cleaning=True, docked=False, cleaning=True)
    # Ready once, then busy for a poll, then ready for good: one stale ready read between
    # dock servicing and the next job must not count.
    h.robot.set_timeline([(0.0, docked), (5.0, busy), (10.0, docked)])
    h.queue.add("Kitchen", KITCHEN_TWICE)
    await h.spin(2)
    assert h.robot.commands == []
    h.robot.set_timeline([(0.0, docked)])
    await h.drain()
    assert h.robot.commands[:3] == KITCHEN_TWICE_COMMANDS


async def test_a_resume_during_the_wind_down_of_a_cancelled_run_is_not_undone():
    h = Harness()
    h.queue.add("Kitchen", KITCHEN_TWICE)
    h.queue.add("Hall", HALL_MOP)
    while ("clean_rooms", (17,), 2) not in h.robot.commands:
        await asyncio.sleep(0)
    h.queue.hold(STOPPED_BY_USER)
    h.slot.cancel.set()
    h.queue.resume()  # tapped before the run has reported its cancel
    await h.drain()
    assert _state(h.queue) == _idle()
    assert h.robot.commands[-3:] == HALL_MOP_COMMANDS
