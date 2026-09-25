"""P6: RoborockCloudAdapter mapping and command wiring, via mocks (no account).

Pins the state-code -> DeviceStatus mapping and asserts each command method sends the exact
RoborockCommand and params. This is the only check possible without a real device.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from roborock import RoborockCommand
from roborock.data.v1 import RoborockInCleaning, RoborockStateCode

from roborock_auto.adapters.roborock_cloud.adapter import RoborockCloudAdapter, _pick_device
from roborock_auto.adapters.roborock_cloud.codes import (
    derive_in_cleaning,
    dock_error_str,
    extract_carpet_value,
    is_cleaning,
    is_docked,
    life_pct,
    state_name,
)
from roborock_auto.config import Config
from roborock_auto.domain.models import CarpetMode, DeviceStatus, FanPower, MopMode, WaterMode


def _config(**over) -> Config:
    base = dict(
        gateway="live", email="e@x.com", duid="DUID1",
        carpet_rise_value=2, carpet_avoid_value=0, poll_interval_sec=5.0, app_port=8000,
        session_path="user_data.json", database_path=":memory:", timezone="Australia/Sydney",
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


def test_is_cleaning_allowlist_fails_safe():
    # Genuine out-cleaning states classify as cleaning...
    assert is_cleaning(RoborockStateCode.cleaning)
    assert is_cleaning(RoborockStateCode.segment_cleaning)
    assert is_cleaning(RoborockStateCode.segment_mopping)
    assert is_cleaning(RoborockStateCode.going_to_target)
    # ...dock servicing and transit do NOT (they lingered from the previous stage and used
    # to be mistaken for departure)...
    assert not is_cleaning(RoborockStateCode.emptying_the_bin)
    assert not is_cleaning(RoborockStateCode.washing_the_mop)
    assert not is_cleaning(RoborockStateCode.returning_home)
    assert not is_cleaning(RoborockStateCode.charging)
    # ...and an unmapped/unknown state fails safe: not cleaning, so departure won't falsely
    # confirm on a firmware state this library version doesn't know.
    assert not is_cleaning(None)


def test_in_cleaning_survives_midjob_recharge():
    # state flips to charging during a recharge, but the flag says job not complete.
    assert derive_in_cleaning(RoborockStateCode.charging, RoborockInCleaning.segment_clean_not_complete)
    # genuinely done: docked and flag complete.
    assert not derive_in_cleaning(RoborockStateCode.charging, RoborockInCleaning.complete)
    # active states (incl. Qrevo mopping + dock servicing) with no flag still count as busy.
    assert derive_in_cleaning(RoborockStateCode.segment_cleaning, None)
    assert derive_in_cleaning(RoborockStateCode.segment_mopping, None)
    assert derive_in_cleaning(RoborockStateCode.washing_the_mop, None)
    assert derive_in_cleaning(RoborockStateCode.docking, None)
    assert not derive_in_cleaning(RoborockStateCode.idle, None)
    # KNOWN LIMITATION: charging + no flag reads as done. If the firmware ever drops the
    # flag during a mid-job recharge, this would mis-declare completion.
    assert not derive_in_cleaning(RoborockStateCode.charging, None)


def test_life_pct():
    assert life_pct(0, 300) == 100
    assert life_pct(150 * 3600, 300) == 50
    assert life_pct(400 * 3600, 300) == 0  # over lifetime clamps to 0
    assert life_pct(None, 300) == 100


def test_extract_carpet_value():
    assert extract_carpet_value([{"carpet_clean_mode": 2}]) == 2
    assert extract_carpet_value({"carpet_clean_mode": 0}) == 0
    assert extract_carpet_value([5]) == 5


def test_dock_error_str():
    from roborock.data.v1.v1_code_mappings import RoborockDockErrorCode

    assert dock_error_str(None) is None
    assert dock_error_str(RoborockDockErrorCode.ok) is None
    # The two wash-dock tank errors get friendly phrasing...
    assert dock_error_str(RoborockDockErrorCode.water_empty) == "clean water tank empty"
    assert dock_error_str(RoborockDockErrorCode.waste_water_tank_full) == "waste water tank full"
    # ...any other code still surfaces, as its de-underscored enum name.
    assert dock_error_str(RoborockDockErrorCode.duct_blockage) == "duct blockage"


def test_pick_device_by_duid():
    from types import SimpleNamespace
    a = SimpleNamespace(duid="A", v1_properties=object())
    b = SimpleNamespace(duid="B", v1_properties=object())
    assert _pick_device([a, b], "B") is b
    with pytest.raises(ValueError, match="no device"):
        _pick_device([a, b], "Z")


# --- command wiring -------------------------------------------------------

@pytest.fixture
def gw():
    g = RoborockCloudAdapter(_config())
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


async def test_stop_and_find_robot(gw):
    await gw.stop()
    assert _last_send(gw).args[0] == RoborockCommand.APP_STOP
    await gw.find_robot()
    assert _last_send(gw).args[0] == RoborockCommand.FIND_ME


async def test_carpet_sends_configured_value(gw):
    await gw.set_carpet_mode(CarpetMode.RISE)
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.SET_CARPET_CLEAN_MODE
    # Firmware wants a bare object here; the list form is rejected with -10005 "param
    # is not an object" (unlike APP_SEGMENT_CLEAN, which wants the list form).
    assert call.kwargs["params"] == {"carpet_clean_mode": 2}   # carpet_rise_value
    await gw.set_carpet_mode(CarpetMode.AVOID)
    assert _last_send(gw).kwargs["params"] == {"carpet_clean_mode": 0}  # carpet_avoid_value


async def test_set_carpet_rejects_unknown(gw):
    with pytest.raises(ValueError, match="carpet mode"):
        await gw.set_carpet_mode(CarpetMode.UNKNOWN)


async def test_clean_rooms_params(gw):
    await gw.clean_rooms([16, 17])
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.APP_SEGMENT_CLEAN
    assert call.kwargs["params"] == [{"segments": [16, 17], "repeat": 1}]


async def test_clean_rooms_repeat_threads_into_params(gw):
    await gw.clean_rooms([16, 17], repeat=2)
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.APP_SEGMENT_CLEAN
    assert call.kwargs["params"] == [{"segments": [16, 17], "repeat": 2}]


async def test_reset_consumable_calls_trait_with_library_attribute(gw):
    from roborock.devices.traits.v1.consumeable import ConsumableAttribute

    from roborock_auto.domain.models import ConsumablePart

    trait = SimpleNamespace(reset_consumable=AsyncMock())
    gw._device.v1_properties.consumables = trait
    await gw.reset_consumable(ConsumablePart.MAIN_BRUSH)
    trait.reset_consumable.assert_awaited_once_with(ConsumableAttribute.MAIN_BRUSH_WORK_TIME)
    await gw.reset_consumable(ConsumablePart.SIDE_BRUSH)
    trait.reset_consumable.assert_awaited_with(ConsumableAttribute.SIDE_BRUSH_WORK_TIME)
    await gw.reset_consumable(ConsumablePart.FILTER)
    trait.reset_consumable.assert_awaited_with(ConsumableAttribute.FILTER_WORK_TIME)
    await gw.reset_consumable(ConsumablePart.SENSOR)
    trait.reset_consumable.assert_awaited_with(ConsumableAttribute.SENSOR_DIRTY_TIME)


async def test_fan_and_mop(gw):
    await gw.set_fan_power(FanPower.MAX)
    assert _last_send(gw).args[0] == RoborockCommand.SET_CUSTOM_MODE
    await gw.set_mop_mode(MopMode.DEEP)
    assert _last_send(gw).args[0] == RoborockCommand.SET_MOP_MODE


async def test_set_water_mode(gw):
    await gw.set_water_mode(WaterMode.INTENSE)
    call = _last_send(gw)
    assert call.args[0] == RoborockCommand.SET_WATER_BOX_CUSTOM_MODE
    assert call.kwargs["params"] == [203]


async def test_run_routine_uses_web_execute_scene(gw):
    await gw.run_routine(1001)
    gw._web.execute_scene.assert_awaited_once_with(gw._user_data, 1001)


async def test_set_cleaning_mode_calls_status_trait(gw):
    from roborock_auto.domain.models import CleaningMode

    status_trait = SimpleNamespace(set_cleaning_mode=AsyncMock())
    gw._device.v1_properties.status = status_trait
    await gw.set_cleaning_mode(CleaningMode.VACUUM)
    status_trait.set_cleaning_mode.assert_awaited_once_with("vacuum")


async def test_get_carpet_mode_maps_back(gw):
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 2}])
    assert await gw.get_carpet_mode() == CarpetMode.RISE
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 0}])
    assert await gw.get_carpet_mode() == CarpetMode.AVOID
    gw._device.v1_properties.command.send = AsyncMock(return_value=[{"carpet_clean_mode": 99}])
    assert await gw.get_carpet_mode() == CarpetMode.UNKNOWN


async def test_map_ok_flips_on_placeholder_fallback(gw):
    from roborock_auto.adapters.sample_map import SAMPLE_MAP_PNG

    assert gw.map_ok() is True
    # No map_content on the mocked device -> fetch fails -> placeholder + map_ok False.
    assert await gw.get_map_png() == SAMPLE_MAP_PNG
    assert gw.map_ok() is False
    # A later successful fetch clears the flag.
    gw._device.v1_properties.map_content = SimpleNamespace(
        refresh=AsyncMock(), image_content=b"\x89PNG\r\n\x1a\nreal"
    )
    assert await gw.get_map_png() == b"\x89PNG\r\n\x1a\nreal"
    assert gw.map_ok() is True


async def test_map_ok_survives_map_data_failure(gw):
    # A good PNG sets map_ok True; a later failing get_map_data (geometry only) must not
    # flip the placeholder badge over the real map the user still sees.
    gw._device.v1_properties.map_content = SimpleNamespace(
        refresh=AsyncMock(), image_content=b"\x89PNG\r\n\x1a\nreal"
    )
    assert await gw.get_map_png() == b"\x89PNG\r\n\x1a\nreal"
    assert gw.map_ok() is True
    # No map_data on the mocked map_content -> the geometry snapshot is empty.
    assert (await gw.get_map_data()).rooms == []
    assert gw.map_ok() is True


async def test_get_consumables_maps_trait(gw):
    from roborock_auto.domain.models import Consumables

    hours = 3600
    gw._device.v1_properties.consumables = SimpleNamespace(
        refresh=AsyncMock(),
        main_brush_work_time=150 * hours,   # 300 h lifetime -> 50%
        side_brush_work_time=50 * hours,    # 200 h lifetime -> 75%
        filter_work_time=120 * hours,       # 150 h lifetime -> 20%
        sensor_dirty_time=12 * hours,       # 30 h lifetime  -> 60%
        strainer_work_times=75 * hours,     # 150 h lifetime -> 50%
        cleaning_brush_work_times=30 * hours,  # 300 h lifetime -> 90%
    )
    assert await gw.get_consumables() == Consumables(
        main_brush_pct=50, side_brush_pct=75, filter_pct=20,
        sensor_pct=60, strainer_pct=50, cleaning_brush_pct=90,
    )


async def test_get_consumables_dockless_model_reports_none(gw):
    from roborock_auto.domain.models import Consumables

    # A model without a wash dock reports no strainer / cleaning-brush counters; the
    # sensor counter absent reads as fresh (life_pct treats None as unused).
    gw._device.v1_properties.consumables = SimpleNamespace(
        refresh=AsyncMock(),
        main_brush_work_time=0,
        side_brush_work_time=0,
        filter_work_time=0,
        sensor_dirty_time=None,
        strainer_work_times=None,
        cleaning_brush_work_times=None,
    )
    assert await gw.get_consumables() == Consumables(
        main_brush_pct=100, side_brush_pct=100, filter_pct=100,
        sensor_pct=100, strainer_pct=None, cleaning_brush_pct=None,
    )


async def test_get_status_maps_trait(gw):
    from roborock.data.v1.v1_code_mappings import RoborockDockErrorCode

    status_trait = SimpleNamespace(
        refresh=AsyncMock(),
        state=RoborockStateCode.segment_cleaning,
        battery=77,
        in_cleaning=RoborockInCleaning.segment_clean_not_complete,
        error_code=None,
        # StatusTrait's friendly-name helpers and StatusV2's telemetry/dock fields.
        fan_speed_name="balanced",
        mop_route_name="standard",
        # Read by integer, not name: 203 is this firmware's "high", our "intense".
        water_box_mode=203,
        square_meter_clean_area=12.4,
        clean_time=480,
        dock_error_status=RoborockDockErrorCode.water_empty,
    )
    gw._device.v1_properties.status = status_trait
    result = await gw.get_status()
    assert result == DeviceStatus(
        state_name="segment_cleaning", battery=77, in_cleaning=True, docked=False, error=None,
        fan_power="balanced", mop_route="standard", water_mode="intense",
        clean_area_m2=12.4, clean_time_s=480,
        dock_error="clean water tank empty",
        cleaning=True,  # segment_cleaning is a genuine out-cleaning state
    )


async def test_get_status_tolerates_missing_enrichment_fields(gw):
    """An older library/firmware without the enrichment attrs degrades to None, no crash."""
    status_trait = SimpleNamespace(
        refresh=AsyncMock(),
        state=RoborockStateCode.charging,
        battery=100,
        in_cleaning=RoborockInCleaning.complete,
        error_code=None,
    )
    gw._device.v1_properties.status = status_trait
    result = await gw.get_status()
    assert result == DeviceStatus(
        state_name="charging", battery=100, in_cleaning=False, docked=True, error=None,
        fan_power=None, mop_route=None, water_mode=None, clean_area_m2=None, clean_time_s=None,
        dock_error=None, cleaning=False,
    )


@pytest.mark.parametrize("dry_status,expected", [(1, True), (0, False), (None, None), (2, None)])
@pytest.mark.parametrize("mode", ["vacuum", "mop", "vac_and_mop", None])
async def test_status_exposes_independent_drying_and_cleaning_mode(gw, dry_status, expected, mode):
    from dataclasses import asdict

    gw._device.v1_properties.status = SimpleNamespace(
        refresh=AsyncMock(), state=RoborockStateCode.charging, battery=85,
        in_cleaning=RoborockInCleaning.complete, error_code=None,
        dry_status=dry_status, current_cleaning_mode_name=mode,
    )
    assert asdict(await gw.get_status()) == {
        **asdict(DeviceStatus("charging", 85, False, True)),
        "drying_mop": expected, "cleaning_mode": mode,
    }


async def test_map_geometry_matches_png_snapshot_without_second_refresh(gw):
    from roborock_auto.domain.models import MapData, Point

    dims = SimpleNamespace(width=100, height=80, scale=2,
                           to_img=lambda p: SimpleNamespace(x=2 * p.x, y=160 - 2 * p.y))
    mc = SimpleNamespace(refresh=AsyncMock(), image_content=b"png",
                         map_data=SimpleNamespace(image=SimpleNamespace(dimensions=dims),
                                                  rooms={}, vacuum_position=Point(30, 20)))
    gw._device.v1_properties.map_content = mc
    assert await gw.get_map_png() == b"png"
    mc.map_data.vacuum_position = Point(60, 40)
    assert await gw.get_map_data() == MapData(robot=Point(60, 120), image_width=200, image_height=160)
    assert mc.refresh.await_count == 1
    mc.image_content = b""
    await gw.get_map_png()
    assert await gw.get_map_data() == MapData()
    assert mc.refresh.await_count == 2


async def test_geometry_bootstraps_once_and_clears_after_failed_png(gw):
    from roborock_auto.adapters.sample_map import SAMPLE_MAP_PNG
    from roborock_auto.domain.models import MapData, Point

    dims = SimpleNamespace(width=100, height=80, scale=1, to_img=lambda p: p)
    mc = SimpleNamespace(refresh=AsyncMock(), image_content=b"png",
                         map_data=SimpleNamespace(image=SimpleNamespace(dimensions=dims),
                                                  rooms={}, vacuum_position=Point(30, 20)))
    gw._device.v1_properties.map_content = mc
    expected = MapData(robot=Point(30, 20), image_width=100, image_height=80)
    assert await gw.get_map_data() == expected
    assert await gw.get_map_data() == expected
    assert mc.refresh.await_count == 1
    gw._locked_retry = AsyncMock(side_effect=RuntimeError("offline"))
    assert await gw.get_map_png() == SAMPLE_MAP_PNG
    assert await gw.get_map_data() == MapData()
    assert gw.map_ok() is False
