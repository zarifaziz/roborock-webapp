"""The driven adapter over python-roborock: the one package importing roborock.*.

This is the anti-corruption layer. It cannot be exercised without a Roborock login, so it
is quarantined behind the ports and covered by mock tests (test_live_mapping.py) that pin
the command/param each method sends and the state-code -> DeviceStatus mapping. All device
I/O is serialized by a single lock (single user, cheap). Transport-level retry lives here;
workflow-level retry belongs to the use cases.

Command NAMES below are verified against the installed python-roborock. Param SHAPES marked
"confirm on live bring-up" are the community-common form and should be checked on the first
real run via discover; they are isolated here so a wrong guess can't reach the rest of the
app. Code tables and pure translation helpers live in codes.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from roborock import RoborockCommand

from roborock_auto.adapters.roborock_cloud.codes import (
    CLEANING_MODE_NAMES,
    FAN_LEVELS,
    MOP_MODES,
    derive_in_cleaning,
    error_str,
    extract_carpet_value,
    is_docked,
    life_pct,
    state_name,
)
from roborock_auto.adapters.roborock_cloud.session import load_user_data
from roborock_auto.adapters.sample_map import SAMPLE_MAP_PNG
from roborock_auto.config import Config
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
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


class RoborockCloudAdapter:
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

    async def _locked_retry(self, fn, attempts: int = 2, base_delay: float = 1.0) -> Any:
        """Serialize device I/O and retry transient failures.

        Deliberately does NOT call connect() on failure: the cloud login endpoint is rate
        limited (error 9002), and reconnecting on every hiccup turns one failure into a
        storm. Programming errors are re-raised immediately, not retried.
        """
        async with self._lock:
            for i in range(attempts):
                try:
                    return await fn()
                except (AttributeError, TypeError, KeyError):
                    raise
                except Exception:
                    if i == attempts - 1:
                        raise
                    _LOGGER.warning("device call failed, retrying (attempt %d)", i + 1)
                    await asyncio.sleep(base_delay * (2**i))

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
        # Map goes through an unofficial parser; on any failure serve the placeholder so the
        # app never breaks. A mismatched map tells the user the live fetch failed.
        try:
            async def read() -> bytes:
                mc = self._v1.map_content
                await mc.refresh()
                return mc.image_content

            png = await self._locked_retry(read)
            return png or SAMPLE_MAP_PNG
        except Exception:
            _LOGGER.warning("live map fetch failed; serving placeholder", exc_info=True)
            return SAMPLE_MAP_PNG

    async def get_map_data(self) -> MapData:
        try:
            async def read() -> MapData:
                mc = self._v1.map_content
                await mc.refresh()
                data = mc.map_data
                rooms = [Room(int(r.segment_id), getattr(r, "name", None) or f"Room {r.segment_id}")
                         for r in (getattr(data, "rooms", None) or [])]
                robot = None
                pos = getattr(data, "vacuum_position", None)
                if pos is not None:
                    robot = Point(int(getattr(pos, "x", 0)), int(getattr(pos, "y", 0)))
                return MapData(rooms=rooms, robot=robot)

            return await self._locked_retry(read)
        except Exception:
            _LOGGER.warning("live map data fetch failed", exc_info=True)
            return MapData()

    async def list_rooms(self) -> list[Room]:
        async def read() -> list[Room]:
            rooms = self._v1.rooms
            await rooms.refresh()
            return [
                Room(int(m.segment_id), m.name or f"Room {m.segment_id}")
                for m in (rooms.rooms or [])
            ]

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
            # Fields are seconds of use; Roborock's standard lifetimes are 300/200/150 hours.
            return Consumables(
                main_brush_pct=life_pct(getattr(cons, "main_brush_work_time", None), 300),
                side_brush_pct=life_pct(getattr(cons, "side_brush_work_time", None), 200),
                filter_pct=life_pct(getattr(cons, "filter_work_time", None), 150),
            )

        return await self._locked_retry(read)

    async def get_carpet_mode(self) -> CarpetMode:
        # This firmware returns [{"carpet_clean_mode": <int>}].
        resp = await self._send(RoborockCommand.GET_CARPET_CLEAN_MODE)
        value = extract_carpet_value(resp)
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
        await self._send(RoborockCommand.SET_CUSTOM_MODE, [FAN_LEVELS[power]])

    async def set_mop_mode(self, mode: MopMode) -> None:
        await self._send(RoborockCommand.SET_MOP_MODE, [MOP_MODES[mode]])

    async def set_cleaning_mode(self, mode: CleaningMode) -> None:
        # The library computes the correct SET_CLEAN_MOTOR_MODE params for this device.
        async def do() -> None:
            await self._v1.status.set_cleaning_mode(CLEANING_MODE_NAMES[mode])

        await self._locked_retry(do)

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        value = (
            self._config.carpet_rise_value
            if mode == CarpetMode.RISE
            else self._config.carpet_avoid_value
        )
        await self._send(RoborockCommand.SET_CARPET_CLEAN_MODE, [{"carpet_clean_mode": value}])

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
