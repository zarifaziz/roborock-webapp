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
from roborock.map.map_parser import MapParserConfig
from vacuum_map_parser_base.config.drawable import Drawable

from roborock_auto.adapters.map_text import TextRoom, render_map_text
from roborock_auto.adapters.roborock_cloud.codes import (
    CLEANING_MODE_NAMES,
    CONSUMABLE_ATTRS,
    FAN_LEVELS,
    MOP_MODES,
    WATER_MODES,
    derive_in_cleaning,
    dock_error_str,
    drying_mop_value,
    error_str,
    extract_carpet_value,
    is_cleaning,
    is_docked,
    life_pct,
    state_name,
    water_mode_value,
)
from roborock_auto.adapters.roborock_cloud.session import load_user_data
from roborock_auto.config import Config
from roborock_auto.domain.cleaning_history import CleanRecord
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    ConsumablePart,
    Consumables,
    DeviceStatus,
    FanPower,
    MapData,
    MopMode,
    Point,
    Room,
    Routine,
    WaterMode,
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
        self._map_ok = True
        self._map_snapshot: MapData | None = None
        self._map_png = b""
        self._map_config: MapParserConfig | None = None

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        from roborock.devices.device_manager import UserParams, create_device_manager
        from roborock.web_api import RoborockApiClient

        from roborock_auto.adapters.roborock_cloud.palette import install_calm_palette

        # MapParserConfig has no palette field, so the calm map colours are installed
        # by patching the vendor's palette factory before any parser is created.
        install_calm_palette()

        self._user_data = load_user_data(self._config.session_path)
        self._web = RoborockApiClient(username=self._config.email)
        params = UserParams(username=self._config.email, user_data=self._user_data)
        # Render the map at a higher scale than the library default (4) so the big map view
        # stays crisp (room geometry scales by the same factor, so bboxes stay correct), and
        # draw the richer map elements the phone app shows: obstacles (furniture), virtual
        # walls, no-go / no-mop / no-carpet zones, and room-name labels.
        map_config = MapParserConfig(
            map_scale=8,
            # Make the area outside the floor plan transparent instead of the parser's default
            # dark blue, so the app's own map background shows through (no empty blue frame).
            show_background=False,
            drawables=[
                # Draw the mop coverage below the fine vacuum trace and device markers.
                Drawable.MOP_PATH,
                Drawable.PATH,
                Drawable.CHARGER,
                Drawable.VACUUM_POSITION,
                # ROOM_NAMES is intentionally omitted: the parser only knows generic
                # "Room N" labels. The UI overlays the friendly names (from list_rooms).
                Drawable.OBSTACLES,
                Drawable.IGNORED_OBSTACLES,
                Drawable.VIRTUAL_WALLS,
                Drawable.NO_GO_AREAS,
                Drawable.NO_MOPPING_AREAS,
                Drawable.NO_CARPET_AREAS,
                Drawable.ZONES,
            ],
        )
        self._map_config = map_config
        self._manager = await create_device_manager(params, map_parser_config=map_config)
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
        """Serialize device I/O (single MQTT session) and retry transient failures."""
        async with self._lock:
            return await self._retry(fn, attempts, base_delay)

    async def _retry_no_lock(self, fn, attempts: int = 2, base_delay: float = 1.0) -> Any:
        """For web-API calls that do not touch the device connection, so no lock needed."""
        return await self._retry(fn, attempts, base_delay)

    async def _retry(self, fn, attempts: int, base_delay: float) -> Any:
        """Retry transient failures. Deliberately does NOT reconnect: the cloud login
        endpoint is rate limited (error 9002) and reconnecting on every hiccup turns one
        failure into a storm. Programming errors are re-raised immediately.
        """
        for i in range(attempts):
            try:
                return await fn()
            except (AttributeError, TypeError, KeyError):
                raise
            except Exception:
                if i == attempts - 1:
                    raise
                _LOGGER.warning("call failed, retrying (attempt %d)", i + 1)
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
                # Enrichment reads are guarded: a library/firmware missing a field (or a
                # helper property raising on odd data) degrades to None, never a crash.
                fan_power=_opt(st, "fan_speed_name"),
                mop_route=_opt(st, "mop_route_name"),
                water_mode=water_mode_value(_opt(st, "water_box_mode")),
                clean_area_m2=_opt(st, "square_meter_clean_area"),
                clean_time_s=_opt(st, "clean_time"),
                dock_error=dock_error_str(_opt(st, "dock_error_status")),
                cleaning=is_cleaning(st.state),
                cleaning_mode=_opt(st, "current_cleaning_mode_name"),
                drying_mop=drying_mop_value(_opt(st, "dry_status")),
            )

        return await self._locked_retry(read)

    async def get_map_png(self) -> bytes:
        # Preserve the last real image and its geometry through transient cloud failures.
        try:
            async def read() -> bytes:
                mc = self._v1.map_content
                if self._map_config is not None:
                    # The parser shares this list; update in place before rendering.
                    drawables = self._map_config.drawables
                    mode = _opt(_opt(self._v1, "status"), "current_cleaning_mode_name")
                    if Drawable.MOP_PATH in drawables:
                        drawables.remove(Drawable.MOP_PATH)
                    if mode in ("mop", "vac_and_mop"):
                        drawables.insert(0, Drawable.MOP_PATH)
                await mc.refresh()
                png = mc.image_content
                if png:
                    snapshot = _map_data(_opt(mc, "map_data"))
                    self._map_png = png
                    self._map_snapshot = snapshot
                return png

            png = await self._locked_retry(read)
            self._map_ok = bool(png)
        except Exception:
            _LOGGER.warning("live map fetch failed; retaining last real map", exc_info=True)
            self._map_ok = False
        if self._map_snapshot is None:
            self._map_snapshot = MapData()
        return self._map_png

    async def get_map_data(self) -> MapData:
        # Geometry belongs to the displayed PNG, not to a second independently fetched map.
        if self._map_snapshot is None:
            await self.get_map_png()
        return self._map_snapshot or MapData()

    async def get_map_text(self) -> str:
        try:
            async def read() -> str:
                mc = self._v1.map_content
                await mc.refresh()
                data = mc.map_data
                dims = getattr(getattr(data, "image", None), "dimensions", None)
                width = int(dims.width * dims.scale) if dims else 0
                height = int(dims.height * dims.scale) if dims else 0
                if not dims or not width or not height:
                    return "(no map yet)"
                # The parser rooms only know generic "Room N"; join the friendly names.
                try:
                    rooms_trait = self._v1.rooms
                    await rooms_trait.refresh()
                    names = {
                        int(m.segment_id): m.name for m in (rooms_trait.rooms or []) if m.name
                    }
                except Exception:  # noqa: BLE001 - names are a nicety; fall back to Room N
                    names = {}
                raw = getattr(data, "rooms", None) or {}
                items = raw.items() if hasattr(raw, "items") else enumerate(raw)
                rooms = [
                    TextRoom(names.get(int(seg_id)) or f"Room {seg_id}", bbox)
                    for seg_id, r in items
                    if (bbox := _room_bbox(r, dims, width, height)) is not None
                ]
                robot = None
                pos = getattr(data, "vacuum_position", None)
                if pos is not None:
                    try:
                        rp = dims.to_img(pos)
                        robot = (rp.x / width, rp.y / height)
                    except Exception:  # noqa: BLE001 - robot marker is cosmetic
                        robot = None
                # Path.path is a list of segments (each a list of map-coord Points); transform
                # each into the same normalized image space the rooms use.
                path: list[tuple[float, float]] = []
                for seg in getattr(getattr(data, "path", None), "path", None) or []:
                    for p in seg:
                        try:
                            ip = dims.to_img(p)
                            path.append((ip.x / width, ip.y / height))
                        except Exception:  # noqa: BLE001, S112 - a bad point is just skipped
                            continue
                return render_map_text(rooms, robot, path)

            return await self._locked_retry(read)
        except Exception:
            _LOGGER.warning("live map text render failed", exc_info=True)
            return "(map unavailable)"

    def map_ok(self) -> bool:
        return self._map_ok

    async def list_rooms(self) -> list[Room]:
        async def read() -> list[Room]:
            rooms = self._v1.rooms
            await rooms.refresh()
            return [
                Room(int(m.segment_id), m.name or f"Room {m.segment_id}",
                     color=_room_color(int(m.segment_id)))
                for m in (rooms.rooms or [])
            ]

        return await self._locked_retry(read)

    async def list_routines(self) -> list[Routine]:
        async def read() -> list[Routine]:
            scenes = await self._web.get_scenes(self._user_data, self._config.duid)
            return [Routine(int(s.id), s.name) for s in scenes]

        return await self._retry_no_lock(read)

    async def get_last_clean_record(self) -> CleanRecord | None:
        async def read() -> CleanRecord | None:
            summary = self._v1.clean_summary
            await summary.refresh()
            record = summary.last_clean_record
            if record is None:
                return None
            complete, error = _opt(record, "complete"), _opt(record, "error")
            outcome = "unknown"
            if complete == 1 and error == 0:
                outcome = "completed"
            elif complete == 0 or (error is not None and error != 0):
                outcome = "incomplete"
            return CleanRecord(
                started_at=_opt(record, "begin_datetime"),
                ended_at=_opt(record, "end_datetime"),
                duration_s=_opt(record, "duration"),
                area_m2=_opt(record, "square_meter_area"),
                outcome=outcome,
                error_code=error,
            )

        return await self._locked_retry(read)

    async def get_consumables(self) -> Consumables:
        async def read() -> Consumables:
            cons = self._v1.consumables
            await cons.refresh()
            # Fields are seconds of use; Roborock's standard lifetimes are 300/200/150/30
            # hours (brush/brush/filter/sensor). Dock consumables stay None when the
            # firmware doesn't report them, so the UI can hide them.
            def opt_pct(attr: str, max_hours: float) -> int | None:
                work_time = getattr(cons, attr, None)
                return None if work_time is None else life_pct(work_time, max_hours)

            return Consumables(
                main_brush_pct=life_pct(getattr(cons, "main_brush_work_time", None), 300),
                side_brush_pct=life_pct(getattr(cons, "side_brush_work_time", None), 200),
                filter_pct=life_pct(getattr(cons, "filter_work_time", None), 150),
                sensor_pct=life_pct(getattr(cons, "sensor_dirty_time", None), 30),
                strainer_pct=opt_pct("strainer_work_times", 150),
                cleaning_brush_pct=opt_pct("cleaning_brush_work_times", 300),
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

    async def stop(self) -> None:
        await self._send(RoborockCommand.APP_STOP)

    async def dock(self) -> None:
        await self._send(RoborockCommand.APP_CHARGE)

    async def find_robot(self) -> None:
        await self._send(RoborockCommand.FIND_ME)

    async def clean_rooms(self, segment_ids: list[int], repeat: int = 1) -> None:
        # Canonical V1 format (python-roborock / HA / miio all agree): a list holding one object.
        params = [{"segments": segment_ids, "repeat": repeat}]
        _LOGGER.info("APP_SEGMENT_CLEAN payload params=%r", params)
        await self._send(RoborockCommand.APP_SEGMENT_CLEAN, params)

    async def set_fan_power(self, power: FanPower) -> None:
        await self._send(RoborockCommand.SET_CUSTOM_MODE, [FAN_LEVELS[power]])

    async def set_mop_mode(self, mode: MopMode) -> None:
        await self._send(RoborockCommand.SET_MOP_MODE, [MOP_MODES[mode]])

    async def set_water_mode(self, mode: WaterMode) -> None:
        await self._send(RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [WATER_MODES[mode]])

    async def set_cleaning_mode(self, mode: CleaningMode) -> None:
        # The library computes the correct SET_CLEAN_MOTOR_MODE params for this device.
        async def do() -> None:
            await self._v1.status.set_cleaning_mode(CLEANING_MODE_NAMES[mode])

        await self._locked_retry(do)

    async def set_carpet_mode(self, mode: CarpetMode) -> None:
        if mode is CarpetMode.RISE:
            value = self._config.carpet_rise_value
        elif mode is CarpetMode.AVOID:
            value = self._config.carpet_avoid_value
        else:
            raise ValueError(f"cannot set carpet mode to {mode}")
        # Firmware wants a bare object; the list form is rejected with -10005 "param is
        # not an object". (APP_SEGMENT_CLEAN is the opposite — it wants the list form.)
        await self._send(RoborockCommand.SET_CARPET_CLEAN_MODE, {"carpet_clean_mode": value})

    async def reset_consumable(self, part: ConsumablePart) -> None:
        # The trait sends RESET_CONSUMABLE with params=[attribute] and refreshes the counters.
        async def do() -> None:
            await self._v1.consumables.reset_consumable(CONSUMABLE_ATTRS[part])

        await self._locked_retry(do)

    async def run_routine(self, routine_id: int) -> None:
        # Web-API call: retry, but no device lock (it does not touch the device connection).
        async def do() -> None:
            await self._web.execute_scene(self._user_data, routine_id)

        await self._retry_no_lock(do)


def _opt(obj: Any, name: str) -> Any:
    """Read an optional trait attribute; missing attrs and raising properties become None."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - enrichment must never break the status poll
        return None


def _room_bbox(
    room: Any, dims: Any, width: int, height: int
) -> tuple[float, float, float, float] | None:
    """Room box (map coords x0,y0,x1,y1) -> normalized 0..1 fractions of the rendered PNG.

    Uses the parser's own dims.to_img() so scale, the y-flip, and rotation match the image the
    UI shows. Returns None (never raises) when geometry/dimensions are missing so get_map_data's
    fallback stays intact.
    """
    if dims is None or width <= 0 or height <= 0:
        return None
    try:
        from vacuum_map_parser_base.map_data import Point as MPoint

        p0 = dims.to_img(MPoint(room.x0, room.y0))
        p1 = dims.to_img(MPoint(room.x1, room.y1))
    except Exception:  # noqa: BLE001 - unverified geometry must never break the map
        return None

    def _clamp(v: float) -> float:
        return min(1.0, max(0.0, v))

    fx0, fx1 = sorted((_clamp(p0.x / width), _clamp(p1.x / width)))
    fy0, fy1 = sorted((_clamp(p0.y / height), _clamp(p1.y / height)))
    return (fx0, fy0, fx1, fy1)


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


def _room_color(segment_id: int) -> str:
    from roborock_auto.adapters.roborock_cloud.palette import build_palette

    color = build_palette().get_room_color(segment_id)
    return "#" + "".join(f"{channel:02x}" for channel in color[:3])


def _map_data(data: Any) -> MapData:
    try:
        # map_data.rooms is dict[segment_id -> Room(x0,y0,x1,y1,...)]; image.dimensions
        # carries the scale/trim/rotation used to render the PNG.
        dims = getattr(getattr(data, "image", None), "dimensions", None)
        width = int(dims.width * dims.scale) if dims else 0
        height = int(dims.height * dims.scale) if dims else 0
        raw = getattr(data, "rooms", None) or {}
        items = raw.items() if hasattr(raw, "items") else enumerate(raw)
        rooms = [
            Room(
                int(seg_id),
                getattr(r, "name", None) or f"Room {seg_id}",
                bbox=_room_bbox(r, dims, width, height),
                color=_room_color(int(seg_id)),
            )
            for seg_id, r in items
        ]
        robot = None
        pos = getattr(data, "vacuum_position", None)
        if pos is not None and dims is not None:
            try:
                rp = dims.to_img(pos)
                robot = Point(int(rp.x), int(rp.y))
            except Exception:  # noqa: BLE001 - robot marker is cosmetic
                robot = None
        return MapData(rooms=rooms, robot=robot, image_width=width, image_height=height)
    except Exception:
        _LOGGER.warning("live map geometry unavailable", exc_info=True)
        return MapData()
