"""P6: LiveGateway mapping and command wiring, via mocks (no account).

Pins the state-code -> DeviceStatus mapping and asserts each command method sends the exact
RoborockCommand and params. This is the only check possible without a real device.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from roborock import RoborockCommand
from roborock.data.v1 import RoborockInCleaning, RoborockStateCode

from roborock_auto.config import Config
from roborock_auto.gateway.live import (
    LiveGateway,
    derive_in_cleaning,
    is_docked,
    state_name,
)
from roborock_auto.models import CarpetMode, DeviceStatus, FanPower, MopMode


def _config(**over) -> Config:
    base = dict(
        gateway="live", email="e@x.com", duid="DUID1", vacuum_scene_id=1001, mop_scene_id=1002,
        carpet_rise_value=0, carpet_avoid_value=1, poll_interval_sec=5.0, app_port=8000,
        session_path="user_data.json",
    )
    base.update(over)
    return Config(**base)


# --- pure mapping ---------------------------------------------------------

def test_state_name_and_docked():
    assert state_name(RoborockStateCode.charging) == "charging"
    assert state_name(RoborockStateCode.segment_cleaning) == "segment_cleaning"
    assert state_name(None) == "unknown"
    assert is_docked(RoborockStateCode.charging)
    assert is_docked(RoborockStateCode.charging_complete)
    assert not is_docked(RoborockStateCode.cleaning)


def test_in_cleaning_survives_midjob_recharge():
    # state flips to charging during a recharge, but the flag says job not complete.
    assert derive_in_cleaning(RoborockStateCode.charging, RoborockInCleaning.segment_clean_not_complete)
    # genuinely done: docked and flag complete.
    assert not derive_in_cleaning(RoborockStateCode.charging, RoborockInCleaning.complete)
    # active state with no flag still counts.
    assert derive_in_cleaning(RoborockStateCode.segment_cleaning, None)
    assert not derive_in_cleaning(RoborockStateCode.idle, None)


# --- command wiring -------------------------------------------------------

@pytest.fixture
def gw():
    g = LiveGateway(_config())
    g._device = SimpleNamespace(v1_properties=SimpleNamespace(command=SimpleNamespace(send=AsyncMock())))
    g._web = SimpleNamespace(execute_scene=AsyncMock(), get_scenes=AsyncMock())
    g._user_data = object()
    return g


def _last_send(gw):
    return gw._device.v1_properties.command.send.await_args


async def test_start_pause_dock(gw):
    await gw.start()
    assert _last_send(gw).args[0] == RoborockCommand.APP_START
    await gw.pause()
    assert _last_send(gw).args[0] == RoborockCommand.APP_PAUSE
    await gw.dock()
    assert _last_send(gw).args[0] == RoborockCommand.APP_CHARGE


async def test_carpet_sends_configured_value(gw):
    await gw.set_carpet_mode(CarpetMode.RISE)
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.SET_CARPET_CLEAN_MODE
    assert call.kwargs["params"] == [{"carpet_clean_mode": 0}]   # carpet_rise_value
    await gw.set_carpet_mode(CarpetMode.AVOID)
    assert _last_send(gw).kwargs["params"] == [{"carpet_clean_mode": 1}]  # carpet_avoid_value


async def test_clean_rooms_params(gw):
    await gw.clean_rooms([16, 17])
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.APP_SEGMENT_CLEAN
    assert call.kwargs["params"] == [{"segments": [16, 17], "repeat": 1}]


async def test_fan_and_mop(gw):
    await gw.set_fan_power(FanPower.MAX)
    assert _last_send(gw).args[0] == RoborockCommand.SET_CUSTOM_MODE
    await gw.set_mop_mode(MopMode.DEEP)
    assert _last_send(gw).args[0] == RoborockCommand.SET_MOP_MODE


async def test_run_routine_uses_web_execute_scene(gw):
    await gw.run_routine(1001)
    gw._web.execute_scene.assert_awaited_once_with(gw._user_data, 1001)


async def test_set_cleaning_mode_calls_status_trait(gw):
    from roborock_auto.models import CleaningMode

    status_trait = SimpleNamespace(set_cleaning_mode=AsyncMock())
    gw._device.v1_properties.status = status_trait
    await gw.set_cleaning_mode(CleaningMode.VACUUM)
    status_trait.set_cleaning_mode.assert_awaited_once_with("vacuum")


async def test_get_carpet_mode_maps_back(gw):
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 0}])
    assert await gw.get_carpet_mode() == CarpetMode.RISE
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 1}])
    assert await gw.get_carpet_mode() == CarpetMode.AVOID
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 99}])
    assert await gw.get_carpet_mode() == CarpetMode.UNKNOWN


async def test_get_status_maps_trait(gw):
    status_trait = SimpleNamespace(
        refresh=AsyncMock(),
        state=RoborockStateCode.segment_cleaning,
        battery=77,
        in_cleaning=RoborockInCleaning.segment_clean_not_complete,
        error_code=None,
    )
    gw._device.v1_properties.status = status_trait
    result = await gw.get_status()
    assert result == DeviceStatus(
        state_name="segment_cleaning", battery=77, in_cleaning=True, docked=False, error=None
    )
