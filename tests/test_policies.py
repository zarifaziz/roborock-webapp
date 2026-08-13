"""Domain policies: the status-interpretation rules, stated as documentation."""

from __future__ import annotations

from roborock_auto.domain.models import DeviceStatus
from roborock_auto.domain.policies import home_and_idle, job_is_active, ready_to_start


def _status(**over) -> DeviceStatus:
    base = dict(state_name="charging", battery=100, in_cleaning=False, docked=True, error=None)
    base.update(over)
    return DeviceStatus(**base)


def test_midjob_recharge_keeps_job_active():
    recharging = _status(in_cleaning=True, docked=True, battery=20)
    assert job_is_active(recharging) is True
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


def test_ready_to_start_requires_docked_idle_and_no_error():
    assert ready_to_start(_status()) is True
    assert ready_to_start(_status(in_cleaning=True)) is False
    assert ready_to_start(_status(docked=False, state_name="idle")) is False
    assert ready_to_start(_status(error="brush stuck")) is False
