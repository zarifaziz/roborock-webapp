"""Domain policies: the status-interpretation rules, stated as documentation."""

from __future__ import annotations

from roborock_auto.domain.models import DeviceStatus
from roborock_auto.domain.policies import home_and_idle, ready_to_start


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
