"""Domain policies: the status-interpretation rules and the Overdue scoring rules,
stated as documentation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from roborock_auto.domain.models import DeviceStatus, RoomInterval, SegmentOverdue
from roborock_auto.domain.policies import (
    clean_priority,
    home_and_idle,
    overdue_report,
    ready_to_start,
)


def _status(**over) -> DeviceStatus:
    base = dict(state_name="charging", battery=100, in_cleaning=False, docked=True, error=None)
    base.update(over)
    return DeviceStatus(**base)


def test_midjob_recharge_is_not_home_and_idle():
    # docked + charging but in_cleaning: the job is not done, so the mop stage must not start.
    recharging = _status(in_cleaning=True, docked=True, battery=20)
    assert home_and_idle(recharging) is False


def test_home_and_idle_is_docked_and_not_cleaning():
    assert home_and_idle(_status()) is True
    assert home_and_idle(_status(docked=False, state_name="cleaning", in_cleaning=True)) is False


def test_job_stalled_on_pause_or_cancel_off_dock():
    from roborock_auto.domain.policies import job_stalled

    assert job_stalled(_status(state_name="paused", in_cleaning=True, docked=False)) is True
    assert job_stalled(_status(state_name="idle", in_cleaning=False, docked=False)) is True
    assert job_stalled(_status(state_name="cleaning", in_cleaning=True, docked=False)) is False
    assert job_stalled(_status()) is False  # home and idle is done, not stalled


def test_indeterminate_states_are_not_stalled():
    # unknown / device_offline / locked mean "no information", not "cancelled off-dock".
    # A >5 min wifi/cloud blip must not abort a healthy run as a stall.
    from roborock_auto.domain.policies import job_stalled

    assert job_stalled(_status(state_name="unknown", in_cleaning=False, docked=False)) is False
    assert (
        job_stalled(_status(state_name="device_offline", in_cleaning=False, docked=False)) is False
    )
    assert job_stalled(_status(state_name="locked", in_cleaning=False, docked=False)) is False
    # A genuine off-dock idle (user cancel) still stalls.
    assert job_stalled(_status(state_name="idle", in_cleaning=False, docked=False)) is True


def test_recharging_pending_is_docked_and_still_in_cleaning():
    from roborock_auto.domain.policies import recharging_pending

    # Low battery: the clean was accepted but the robot charges before leaving.
    assert recharging_pending(_status(in_cleaning=True, docked=True, battery=15)) is True
    assert recharging_pending(_status()) is False  # docked idle: nothing pending
    assert (
        recharging_pending(_status(state_name="cleaning", in_cleaning=True, docked=False)) is False
    )


def test_ready_to_start_requires_docked_idle_and_no_error():
    assert ready_to_start(_status()) is True
    assert ready_to_start(_status(in_cleaning=True)) is False
    assert ready_to_start(_status(docked=False, state_name="idle")) is False
    assert ready_to_start(_status(error="brush stuck")) is False


NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


def _entry(segment_id: int, days: float, cleaned_days_ago: float | None) -> SegmentOverdue:
    """The Overdue entry a Segment on a `days`-long Interval, last cleaned
    `cleaned_days_ago` ago, has at NOW."""
    return SegmentOverdue(
        segment_id=segment_id,
        interval=timedelta(days=days),
        last_cleaned_at=None if cleaned_days_ago is None else NOW - timedelta(days=cleaned_days_ago),
        priority=None if cleaned_days_ago is None else cleaned_days_ago / days,
    )


def test_room_interval_must_be_a_positive_duration():
    with pytest.raises(ValueError, match="positive duration"):
        RoomInterval(segment_id=16, interval=timedelta(0))
    with pytest.raises(ValueError, match="positive duration"):
        RoomInterval(segment_id=16, interval=timedelta(days=-1))


def test_sub_day_intervals_are_expressible():
    twice_daily = RoomInterval(segment_id=16, interval=timedelta(hours=12))
    assert clean_priority(twice_daily.interval, NOW - timedelta(hours=18), NOW) == 1.5


def test_priority_is_elapsed_over_the_segments_own_interval():
    # A bedroom on 7 days and a kitchen on 2 days compare directly: both cleaned 3 days
    # ago, the kitchen scores higher because 3 days is more of its own interval.
    assert clean_priority(timedelta(days=7), NOW - timedelta(days=3), NOW) == 3 / 7
    assert clean_priority(timedelta(days=2), NOW - timedelta(days=3), NOW) == 1.5


def test_a_never_cleaned_segment_has_no_finite_priority():
    assert clean_priority(timedelta(days=2), None, NOW) is None


def test_overdue_report_is_sorted_worst_first_with_never_cleaned_on_top():
    intervals = [
        RoomInterval(16, timedelta(days=7)),   # 3/7 of its interval elapsed
        RoomInterval(17, timedelta(days=2)),   # 1.5 intervals elapsed: overdue
        RoomInterval(18, timedelta(days=1)),   # never cleaned
        RoomInterval(19, timedelta(days=4)),   # 1 interval elapsed exactly: overdue
    ]
    last_cleaned = {
        16: NOW - timedelta(days=3),
        17: NOW - timedelta(days=3),
        19: NOW - timedelta(days=4),
    }
    assert overdue_report(intervals, last_cleaned, NOW) == [
        _entry(18, days=1, cleaned_days_ago=None),
        _entry(17, days=2, cleaned_days_ago=3),
        _entry(19, days=4, cleaned_days_ago=4),
        _entry(16, days=7, cleaned_days_ago=3),
    ]


def test_never_cleaned_segments_sort_above_everything_and_tie_on_segment_number():
    intervals = [RoomInterval(18, timedelta(days=1)), RoomInterval(17, timedelta(days=2))]
    assert overdue_report(intervals, {}, NOW) == [
        _entry(17, days=2, cleaned_days_ago=None),
        _entry(18, days=1, cleaned_days_ago=None),
    ]


def test_overdue_entries_flag_never_cleaned_and_whether_they_are_due():
    never, due, fresh = overdue_report(
        [RoomInterval(16, timedelta(days=2)), RoomInterval(17, timedelta(days=2)),
         RoomInterval(18, timedelta(days=2))],
        {17: NOW - timedelta(days=2), 18: NOW - timedelta(days=1)},
        NOW,
    )
    assert (never.never_cleaned, never.overdue) == (True, True)
    assert (due.never_cleaned, due.overdue) == (False, True)      # exactly one interval
    assert (fresh.never_cleaned, fresh.overdue) == (False, False)


def test_a_segment_with_no_interval_is_not_in_the_report():
    assert overdue_report([], {16: NOW}, NOW) == []
