"""Driving adapter: FastAPI control panel over the ports.

One shared gateway is created in the lifespan handler and reused across requests. The map
PNG is only re-read when the parsed map data changes, so polling stays cheap.
"""

from __future__ import annotations

import dataclasses
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from roborock_auto.adapters import get_gateway
from roborock_auto.config import Config
from roborock_auto.domain.capabilities import QREVO_L_PRO
from roborock_auto.domain.models import CarpetMode, FanPower, MopMode
from roborock_auto.ports import RobotGateway

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config.from_env()
    gw = get_gateway(config)
    await gw.connect()
    app.state.gateway = gw
    app.state.config = config
    app.state.map_cache = {"at": 0.0, "png": b""}
    try:
        yield
    finally:
        await gw.close()


app = FastAPI(title="roborock-auto", lifespan=lifespan)


def _gw(request: Request) -> RobotGateway:
    return request.app.state.gateway


@app.get("/capabilities")
async def capabilities():
    caps = dataclasses.asdict(QREVO_L_PRO)
    caps["carpet_modes"] = sorted(m.value for m in QREVO_L_PRO.carpet_modes)
    return caps


@app.get("/status")
async def status(request: Request):
    return dataclasses.asdict(await _gw(request).get_status())


@app.get("/map-data")
async def map_data(request: Request):
    return dataclasses.asdict(await _gw(request).get_map_data())


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


@app.post("/start")
async def start(request: Request):
    await _gw(request).start()
    return {"ok": True}


@app.post("/pause")
async def pause(request: Request):
    await _gw(request).pause()
    return {"ok": True}


@app.post("/dock")
async def dock(request: Request):
    await _gw(request).dock()
    return {"ok": True}


@app.post("/clean/rooms")
async def clean_rooms(request: Request, segment_ids: list[int] = Body(..., embed=True)):
    await _gw(request).clean_rooms(segment_ids)
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


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
