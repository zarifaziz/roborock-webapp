"""Driving adapter: FastAPI control panel over the ports.

One shared gateway is created in the lifespan handler and reused across requests. The map
PNG is only re-read when the parsed map data changes, so polling stays cheap.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from roborock_auto.adapters import get_auth, get_gateway
from roborock_auto.adapters.sqlite import SqliteCleaningLog, open_database
from roborock_auto.application.start_clean import start_clean
from roborock_auto.application.two_stage_clean import run_two_stage
from roborock_auto.config import Config
from roborock_auto.domain.capabilities import QREVO_L_PRO
from roborock_auto.domain.models import (
    CarpetMode,
    CleaningMode,
    CleanSource,
    ConsumablePart,
    FanPower,
    MopMode,
    WaterMode,
)
from roborock_auto.ports import RobotGateway
from roborock_auto.ports.auth import AuthPort

STATIC_DIR = Path(__file__).parent / "static"
_LOGGER = logging.getLogger("roborock_auto.two_stage")

# Human-readable message per two-stage stage token, surfaced on the status endpoint.
_STAGE_MESSAGES = {
    "vacuuming": "Vacuuming (carpet raised)",
    "vacuum_waiting": "Vacuum running, waiting for the robot to return",
    "mopping": "Mopping (carpet avoided)",
    "mop_waiting": "Mop running, waiting for the robot to return",
    "done": "Two-stage clean complete",
}


def _idle_progress() -> dict:
    return {"running": False, "stage": "idle", "message": "", "error": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config.from_env()
    gw = get_gateway(config)
    await gw.connect()
    db = open_database(config.database_path)
    app.state.gateway = gw
    app.state.auth = get_auth(config)
    app.state.config = config
    app.state.cleaning_log = SqliteCleaningLog(db, device_id=config.duid or "fake")
    app.state.map_cache = {"at": 0.0, "png": b""}
    app.state.two_stage = _idle_progress()
    app.state.two_stage_task = None
    try:
        yield
    finally:
        task = app.state.two_stage_task
        if task is not None:
            task.cancel()
        await gw.close()
        db.close()


app = FastAPI(title="roborock-webapp", lifespan=lifespan)


def _gw(request: Request) -> RobotGateway:
    return request.app.state.gateway


def _auth(request: Request) -> AuthPort:
    return request.app.state.auth


@app.get("/capabilities")
async def capabilities():
    caps = dataclasses.asdict(QREVO_L_PRO)
    caps["carpet_modes"] = sorted(m.value for m in QREVO_L_PRO.carpet_modes)
    caps["model"] = "Qrevo L Pro"
    return caps


@app.get("/status")
async def status(request: Request):
    gw = _gw(request)
    body = dataclasses.asdict(await gw.get_status())
    # Piggybacked on the status poll so a placeholder map is visible in the UI without
    # an extra device call (map_ok reads adapter state, no I/O).
    body["map_ok"] = gw.map_ok()
    return body


@app.get("/map-data")
async def map_data(request: Request):
    return dataclasses.asdict(await _gw(request).get_map_data())


@app.get("/map.txt", response_class=PlainTextResponse)
async def map_txt(request: Request):
    """A compact text/ASCII map — rooms, robot position, driven-path coverage — cheap to read
    (curl it, or an agent reads it) instead of decoding the PNG."""
    return await _gw(request).get_map_text()


_MAP_TTL_SEC = 15.0  # the map is fetched at most this often, independent of status polling


@app.get("/map.png")
async def map_png(request: Request):
    # A map fetch holds the single device lock for seconds; refresh on a TTL so it does not
    # queue behind every 5s status poll and slow down start/pause/dock.
    cache = request.app.state.map_cache
    now = time.monotonic()
    if not cache["png"] or now - cache["at"] >= _MAP_TTL_SEC:
        cache["png"] = await _gw(request).get_map_png()
        cache["at"] = now
    return Response(content=cache["png"], media_type="image/png")


@app.get("/rooms")
async def rooms(request: Request):
    return [dataclasses.asdict(r) for r in await _gw(request).list_rooms()]


@app.get("/routines")
async def routines(request: Request):
    return [dataclasses.asdict(r) for r in await _gw(request).list_routines()]


@app.get("/consumables")
async def consumables(request: Request):
    return dataclasses.asdict(await _gw(request).get_consumables())


@app.post("/consumables/reset")
async def consumables_reset(request: Request, part: str = Body(..., embed=True)):
    await _gw(request).reset_consumable(_parse(ConsumablePart, part))
    return {"ok": True}


@app.post("/start")
async def start(request: Request, mode: str = Body(..., embed=True)):
    await start_clean(
        _gw(request),
        cleaning_log=request.app.state.cleaning_log,
        source=CleanSource.APP,
        mode=_parse(CleaningMode, mode),
        segment_ids=None,
    )
    return {"ok": True}


@app.post("/resume")
async def resume(request: Request):
    # Resume is the same firmware command as start, but it continues the paused job:
    # no mode change and no new Cleaning Run.
    await _gw(request).start()
    return {"ok": True}


@app.post("/pause")
async def pause(request: Request):
    await _gw(request).pause()
    return {"ok": True}


@app.post("/stop")
async def stop(request: Request):
    await _gw(request).stop()
    return {"ok": True}


@app.post("/dock")
async def dock(request: Request):
    await _gw(request).dock()
    return {"ok": True}


@app.post("/find")
async def find(request: Request):
    await _gw(request).find_robot()
    return {"ok": True}


def _parse(enum_cls, value: str):
    try:
        return enum_cls(value)
    except ValueError:
        raise HTTPException(422, f"invalid value {value!r} for {enum_cls.__name__}")


@app.post("/fan")
async def fan(request: Request, power: str = Body(..., embed=True)):
    await _gw(request).set_fan_power(_parse(FanPower, power))
    return {"ok": True}


@app.post("/mop")
async def mop(request: Request, mode: str = Body(..., embed=True)):
    await _gw(request).set_mop_mode(_parse(MopMode, mode))
    return {"ok": True}


@app.post("/water")
async def water(request: Request, mode: str = Body(..., embed=True)):
    await _gw(request).set_water_mode(_parse(WaterMode, mode))
    return {"ok": True}


@app.get("/carpet")
async def carpet_get(request: Request):
    # One boot-time read so the Setup carpet toggle can show the device's current setting;
    # deliberately NOT part of the status poll (it costs a device command each call).
    return {"mode": (await _gw(request).get_carpet_mode()).value}


@app.post("/carpet")
async def carpet(request: Request, mode: str = Body(..., embed=True)):
    parsed = _parse(CarpetMode, mode)
    if parsed is CarpetMode.UNKNOWN:
        raise HTTPException(422, "cannot set carpet mode to 'unknown'")
    await _gw(request).set_carpet_mode(parsed)
    return {"ok": True}


@app.post("/routine")
async def routine(request: Request, routine_id: int = Body(..., embed=True)):
    await _gw(request).run_routine(routine_id)
    return {"ok": True}


def _require_nonempty(**fields: str) -> None:
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(422, f"{name} must not be empty")


@app.get("/auth/status")
async def auth_status(request: Request):
    config = request.app.state.config
    if config.gateway == "fake":
        return {"connected": True, "mode": "fake", "email": None}
    return {
        "connected": Path(config.session_path).exists(),
        "mode": "live",
        "email": config.email or None,
    }


@app.post("/auth/request-code")
async def auth_request_code(request: Request, email: str = Body(..., embed=True)):
    _require_nonempty(email=email)
    await _auth(request).request_code(email)
    return {"ok": True}


@app.post("/auth/submit-code")
async def auth_submit_code(
    request: Request,
    email: str = Body(..., embed=True),
    code: str = Body(..., embed=True),
):
    _require_nonempty(email=email, code=code)
    await _auth(request).submit_code(email, code)
    return {"ok": True}


@app.get("/auth/discover")
async def auth_discover(request: Request):
    gw = _gw(request)
    config = request.app.state.config
    carpet = await gw.get_carpet_mode()
    return {
        "mode": config.gateway,
        "carpet_mode": carpet.value,
        "rooms": [dataclasses.asdict(r) for r in await gw.list_rooms()],
        "routines": [dataclasses.asdict(r) for r in await gw.list_routines()],
        "consumables": dataclasses.asdict(await gw.get_consumables()),
    }


@app.post("/clean/segments")
async def clean_segments(
    request: Request,
    segment_ids: list[int] = Body(..., embed=True),
    mode: str = Body(..., embed=True),
    repeat: int = Body(1, embed=True),
):
    if not segment_ids:
        raise HTTPException(422, "segment_ids must not be empty")
    if not 1 <= repeat <= 3:
        raise HTTPException(422, "repeat must be between 1 and 3")
    await start_clean(
        _gw(request),
        cleaning_log=request.app.state.cleaning_log,
        source=CleanSource.APP,
        mode=_parse(CleaningMode, mode),
        segment_ids=segment_ids,
        repeat=repeat,
    )
    return {"ok": True}


@app.post("/clean/two-stage")
async def clean_two_stage(request: Request, segment_ids: list[int] = Body(None, embed=True)):
    progress = request.app.state.two_stage
    if progress["running"]:
        raise HTTPException(409, "a two-stage clean is already running")
    gw = _gw(request)
    config = request.app.state.config
    progress.update(running=True, stage="vacuuming", message="starting", error=None)

    def on_stage(stage: str) -> None:
        progress["stage"] = stage
        progress["message"] = _STAGE_MESSAGES.get(stage, "")

    async def runner() -> None:
        try:
            await run_two_stage(
                gw, config,
                cleaning_log=request.app.state.cleaning_log, source=CleanSource.APP,
                segment_ids=segment_ids or None, on_stage=on_stage, log=_LOGGER.info,
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the status endpoint
            progress.update(stage="error", message=str(exc), error=str(exc))
        finally:
            progress["running"] = False

    request.app.state.two_stage_task = asyncio.create_task(runner())
    return {"ok": True}


@app.get("/clean/two-stage/status")
async def clean_two_stage_status(request: Request):
    return dict(request.app.state.two_stage)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
