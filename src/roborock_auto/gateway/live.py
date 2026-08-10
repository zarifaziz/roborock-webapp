"""The real gateway over python-roborock. The one account-dependent piece.

Cannot be exercised without a Roborock login, so it is quarantined behind the Gateway seam
and covered by mock tests (test_live_mapping.py) that pin the command/param each method sends
and the state-code -> DeviceStatus mapping. All device I/O is serialized by a single lock
(single user, cheap) and the connection is rebuilt on a dropped session.

Command NAMES below are verified against the installed python-roborock. Param SHAPES marked
"confirm on live bring-up" are the community-common form and should be checked on the first
real run via discover.py; they are isolated here so a wrong guess can't reach the rest of the
app.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from roborock import RoborockCommand
from roborock.data.v1 import RoborockInCleaning, RoborockStateCode

from roborock_auto.config import Config
from roborock_auto.core import load_user_data
from roborock_auto.models import (
    CarpetMode,
    Consumables,
    DeviceStatus,
    FanPower,
    MapData,
    MopMode,
    Point,
    Room,
    Routine,
)

_LOGGER = logging.getLogger(__name__)

# Docked = charger connected. Codes from roborock.data.v1.RoborockStateCode.
_DOCKED_STATES = {
    RoborockStateCode.charging,
    RoborockStateCode.charging_complete,
    RoborockStateCode.charging_problem,
}
_ACTIVE_STATES = {
    RoborockStateCode.cleaning,
    RoborockStateCode.spot_cleaning,
    RoborockStateCode.zoned_cleaning,
    RoborockStateCode.segment_cleaning,
    RoborockStateCode.returning_home,
    RoborockStateCode.going_to_target,
}

# Param shapes to confirm on live bring-up. Non-destructive settings; wrong values only
# misconfigure cleaning intensity, they cannot damage anything.
_FAN_LEVELS = {FanPower.QUIET: 101, FanPower.BALANCED: 102, FanPower.TURBO: 103, FanPower.MAX: 104}
_MOP_MODES = {MopMode.STANDARD: 300, MopMode.DEEP: 301}


def state_name(code: RoborockStateCode | None) -> str:
    if code is None:
        return "unknown"
    return code.name


def is_docked(code: RoborockStateCode | None) -> bool:
    return code in _DOCKED_STATES


def derive_in_cleaning(code: RoborockStateCode | None, flag: RoborockInCleaning | None) -> bool:
    """A job is active if the in_cleaning flag says so (this survives a mid-job recharge,
    where the state flips to charging but the flag stays non-complete), or the state itself
    is an active-cleaning state.
    """
    if flag is not None and flag != RoborockInCleaning.complete:
        return True
    return code in _ACTIVE_STATES


def error_str(error_code: Any) -> str | None:
    if error_code is None:
        return None
    name = getattr(error_code, "name", str(error_code))
    return None if name in ("none", "ok", "0") else name


class LiveGateway:
    def __init__(self, config: Config):
        self._config = config
        self._lock = asyncio.Lock()
        self._manager: Any = None
        self._device: Any = None
        self._web: Any = None
        self._user_data: Any = None

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        from roborock.devices.device_manager import UserParams, create_device_manager
        from roborock.web_api import RoborockApiClient

        self._user_data = load_user_data(self._config.session_path)
        self._web = RoborockApiClient(username=self._config.email)
        params = UserParams(username=self._config.email, user_data=self._user_data)
        self._manager = await create_device_manager(params)
        devices = await self._manager.get_devices()
        self._device = _pick_device(devices, self._config.duid)

    async def close(self) -> None:
        if self._manager is not None:
            await self._manager.close()

    @property
    def _v1(self) -> Any:
        return self._device.v1_properties

    async def _send(self, command: RoborockCommand, params: Any = None) -> Any:
        async def once() -> Any:
            return await self._v1.command.send(command, params=params)

        return await self._locked_retry(once)

    async def _locked_retry(self, fn, attempts: int = 3, base_delay: float = 2.0) -> Any:
        async with self._lock:
            for i in range(attempts):
                try:
                    return await fn()
                except Exception:
                    if i == attempts - 1:
                        raise
                    _LOGGER.warning("device call failed, reconnecting (attempt %d)", i + 1)
                    await asyncio.sleep(base_delay * (2**i))
                    try:
                        await self.connect()
                    except Exception:
                        _LOGGER.exception("reconnect failed")

    # --- reads ------------------------------------------------------------
    async def get_status(self) -> DeviceStatus:
        async def read() -> DeviceStatus:
            st = self._v1.status
            await st.refresh()
            return DeviceStatus(
                state_name=state_name(st.state),
                battery=st.battery or 0,
                in_cleaning=derive_in_cleaning(st.state, st.in_cleaning),
                docked=is_docked(st.state),
                error=error_str(st.error_code),
            )

        return await self._locked_retry(read)

    async def get_map_png(self) -> bytes:
        async def read() -> bytes:
            mc = self._v1.map_content
            await mc.refresh()
            return mc.image_content

        return await self._locked_retry(read)

    async def get_map_data(self) -> MapData:
        async def read() -> MapData:
            mc = self._v1.map_content
            await mc.refresh()
            data = mc.map_data
            rooms = [Room(int(r.segment_id), r.name or f"Room {r.segment_id}")
                     for r in (getattr(data, "rooms", None) or [])]
            robot = None
            pos = getattr(data, "vacuum_position", None) or getattr(data, "robot", None)
            if pos is not None:
                robot = Point(int(getattr(pos, "x", 0)), int(getattr(pos, "y", 0)))
            img = getattr(data, "image", None)
            w = int(getattr(img, "dimensions", None).width) if img else 0
            h = int(getattr(img, "dimensions", None).height) if img else 0
            return MapData(rooms=rooms, robot=robot, image_width=w, image_height=h)

        return await self._locked_retry(read)

    async def list_rooms(self) -> list[Room]:
        async def read() -> list[Room]:
            rooms = self._v1.rooms
            await rooms.refresh()
            return [Room(int(seg), name) for seg, name in (rooms.room_mapping() or {}).items()]

        return await self._locked_retry(read)

    async def list_routines(self) -> list[Routine]:
        async def read() -> list[Routine]:
            scenes = await self._web.get_scenes(self._user_data, self._config.duid)
            return [Routine(int(s.id), s.name) for s in scenes]

        return await self._locked_retry(read)

    async def get_consumables(self) -> Consumables:
        async def read() -> Consumables:
            cons = self._v1.consumables
            await cons.refresh()
            return Consumables(
                main_brush_pct=int(getattr(cons, "main_brush_life", 0) or 0),
                side_brush_pct=int(getattr(cons, "side_brush_life", 0) or 0),
                filter_pct=int(getattr(cons, "filter_life", 0) or 0),
            )

        return await self._locked_retry(read)

    async def get_carpet_mode(self) -> CarpetMode:
        resp = await self._send(RoborockCommand.GET_CARPET_CLEAN_MODE)
        value = resp[0] if isinstance(resp, list) and resp else resp
        if value == self._config.carpet_rise_value:
            return CarpetMode.RISE
        if value == self._config.carpet_avoid_value:
            return CarpetMode.AVOID
        return CarpetMode.UNKNOWN

    # --- commands ---------------------------------------------------------
    async def start(self) -> None:
        await self._send(RoborockCommand.APP_START)

    async def pause(self) -> None:
        await self._send(RoborockCommand.APP_PAUSE)

    async def dock(self) -> None:
        await self._send(RoborockCommand.APP_CHARGE)

    async def clean_rooms(self, segment_ids: list[int]) -> None:
        # confirm on live bring-up: some firmwares want [{"segments": [...], "repeat": 1}]
        await self._send(RoborockCommand.APP_SEGMENT_CLEAN, [{"segments": segment_ids, "repeat": 1}])

    async def set_fan_power(self, power: FanPower) -> None:
        await self._send(RoborockCommand.SET_CUSTOM_MODE, [_FAN_LEVELS[power]])

    async def set_mop_mode(self, mode: MopMode) -> None:
        await self._send(RoborockCommand.SET_MOP_MODE, [_MOP_MODES[mode]])

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        value = (
            self._config.carpet_rise_value
            if mode == CarpetMode.RISE
            else self._config.carpet_avoid_value
        )
        await self._send(RoborockCommand.SET_CARPET_CLEAN_MODE, [value])

    async def run_routine(self, routine_id: int) -> None:
        await self._web.execute_scene(self._user_data, routine_id)


def _pick_device(devices: list, duid: str) -> Any:
    if duid:
        for d in devices:
            if d.duid == duid:
                return d
        raise ValueError(f"no device with duid {duid!r}; found {[d.duid for d in devices]}")
    v1 = [d for d in devices if getattr(d, "v1_properties", None) is not None]
    if len(v1) != 1:
        raise ValueError(
            f"set ROBOROCK_DUID; found {len(v1)} V1 devices: {[d.duid for d in v1]}"
        )
    return v1[0]
