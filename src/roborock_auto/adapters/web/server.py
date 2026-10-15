"""Driving adapter: FastAPI control panel over the ports.

One shared gateway is created in the lifespan handler and reused across requests. Nearby
map requests share a short-lived cache to avoid duplicate device reads.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from datetime import time as time_of_day
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from roborock_auto.adapters import get_auth, get_gateway
from roborock_auto.adapters.cleaning_assistant import CleaningAssistant
from roborock_auto.adapters.sqlite import (
    SqliteCleaningLog,
    SqliteOccurrences,
    SqliteRoomIntervals,
    SqliteScheduleRules,
    SqliteSequences,
    open_database,
)
from roborock_auto.adapters.web.assistant_api import router as assistant_router
from roborock_auto.adapters.web.cleaning_context import RecentCleaning
from roborock_auto.application.clean_queue import STOPPED_BY_USER, CleanQueue
from roborock_auto.application.materialise import MATERIALISATION_HORIZON, materialise
from roborock_auto.application.overdue import clean_overdue, overdue_view
from roborock_auto.application.run_slot import RunSlot
from roborock_auto.application.scheduler import Scheduler, readiness_block
from roborock_auto.application.sequence_launch import SequenceLauncher, step_body
from roborock_auto.application.sequence_runner import RunCancelled
from roborock_auto.application.start_clean import SettingsNotConfirmed, start_clean, utc_now
from roborock_auto.application.two_stage_clean import run_two_stage, two_stage_steps
from roborock_auto.config import Config
from roborock_auto.domain.capabilities import QREVO_L_PRO
from roborock_auto.domain.models import (
    CarpetMode,
    CleanAction,
    CleaningMode,
    CleanSettings,
    CleanSource,
    ConsumablePart,
    FanPower,
    MopMode,
    Occurrence,
    OccurrenceState,
    QueuedClean,
    RoomInterval,
    ScheduleRule,
    SegmentOverdue,
    Sequence,
    SequenceAction,
    Step,
    WaterMode,
    Weekday,
    WeeklyGrid,
)
from roborock_auto.domain.policies import ready_to_start
from roborock_auto.ports import RobotGateway
from roborock_auto.ports.auth import AuthPort

STATIC_DIR = Path(__file__).parent / "static"
_LOGGER = logging.getLogger("roborock_auto.two_stage")
# Under launchd's 20s default between SIGTERM and SIGKILL.
_SHUTDOWN_RECOVERY_SEC = 10.0

# Human-readable message per two-stage stage token, surfaced on the status endpoint.
_STAGE_MESSAGES = {
    "vacuuming": "Vacuuming (carpet raised)",
    "vacuum_waiting": "Vacuum running, waiting for the robot to return",
    "mopping": "Mopping (carpet avoided)",
    "mop_waiting": "Mop running, waiting for the robot to return",
    "done": "Two-stage clean complete",
}


def _idle_progress() -> dict:
    return {"stage": "idle", "message": "", "error": None}


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
    app.state.recent_cleaning = RecentCleaning(gw, app.state.cleaning_log)
    app.state.assistant = CleaningAssistant(
        enabled=config.assistant_enabled, model=config.assistant_model,
    )
    app.state.room_intervals = SqliteRoomIntervals(db, device_id=config.duid or "fake")
    app.state.schedule_rules = SqliteScheduleRules(db, device_id=config.duid or "fake")
    app.state.occurrences = SqliteOccurrences(db, device_id=config.duid or "fake")
    app.state.sequences = SqliteSequences(db, device_id=config.duid or "fake")
    app.state.timezone = ZoneInfo(config.timezone)
    # One injected clock for everything that reads or writes a time, so a test can freeze it.
    app.state.now = utc_now
    app.state.map_cache = {"at": 0.0, "png": b""}
    app.state.map_lock = asyncio.Lock()
    app.state.status_lock = asyncio.Lock()
    app.state.status_cache = {"at": None, "status": None, "error": None}
    app.state.run_slot = RunSlot()
    app.state.run_slot.progress.update(_idle_progress())
    app.state.sequence_launcher = SequenceLauncher(
        gw,
        config,
        slot=app.state.run_slot,
        cleaning_log=app.state.cleaning_log,
        now=lambda: app.state.now(),
        log=_LOGGER.info,
    )
    app.state.clean_queue = CleanQueue(
        gw,
        slot=app.state.run_slot,
        launcher=app.state.sequence_launcher,
        interval=config.poll_interval_sec,
        log=_LOGGER.info,
    )
    app.state.scheduler = Scheduler(
        gw,
        rules=app.state.schedule_rules,
        occurrences=app.state.occurrences,
        cleaning_log=app.state.cleaning_log,
        tz=app.state.timezone,
        # Read through app.state on every call, so the test clock reaches the scheduler.
        now=lambda: app.state.now(),
        run_slot=app.state.run_slot,
        sequences=app.state.sequences,
        launcher=app.state.sequence_launcher,
    )
    app.state.scheduler_task = asyncio.create_task(app.state.scheduler.run())
    try:
        yield
    finally:
        app.state.scheduler_task.cancel()
        runs = [
            task for task in (app.state.clean_queue.task, app.state.run_slot.task)
            if task is not None
        ]
        for task in runs:
            task.cancel()
        # A cancelled run docks the robot and resets carpet mode on its way out, which needs
        # the gateway still open. Bounded, so a hung cloud call cannot stall the restart.
        if runs:
            await asyncio.wait(runs, timeout=_SHUTDOWN_RECOVERY_SEC)
        await gw.close()
        db.close()


app = FastAPI(title="roborock-webapp", lifespan=lifespan)
app.include_router(assistant_router)


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


_STATUS_TTL_SEC = 1.0


@app.middleware("http")
async def invalidate_status_after_control(request: Request, call_next):
    try:
        return await call_next(request)
    finally:
        # A cached charging/cleaning state must not outlive a control request.
        if request.method == "POST":
            request.app.state.status_cache["at"] = None


@app.get("/status")
async def status(request: Request):
    gw = _gw(request)
    cache = request.app.state.status_cache
    async with request.app.state.status_lock:
        if cache["at"] is None or time.monotonic() - cache["at"] >= _STATUS_TTL_SEC:
            try:
                cache["status"] = await gw.get_status()
                cache["error"] = None
            except Exception:
                _LOGGER.warning("robot status fetch failed", exc_info=True)
                cache["error"] = "Robot status temporarily unavailable"
            cache["at"] = time.monotonic()
        if cache["error"]:
            raise HTTPException(503, cache["error"])
        body = dataclasses.asdict(cache["status"])
    body["map_ok"] = gw.map_ok()
    return body


@app.get("/map-data")
async def map_data(request: Request):
    # Cold metadata and image requests must share the same initial device fetch.
    if "stale" not in request.app.state.map_cache:
        await map_png(request)
    return dataclasses.asdict(await _gw(request).get_map_data())


@app.get("/map.txt", response_class=PlainTextResponse)
async def map_txt(request: Request):
    """A compact text/ASCII map — rooms, robot position, driven-path coverage — cheap to read
    (curl it, or an agent reads it) instead of decoding the PNG."""
    return await _gw(request).get_map_text()


_MAP_TTL_SEC = 2.0
# A failed fetch can hold the device lock for its full timeout, so back off before the next.
_MAP_RETRY_AFTER_FAILURE_SEC = 30.0


@app.get("/map.png")
async def map_png(request: Request):
    # Coalesce concurrent viewers before they reach the shared device I/O lock. The TTL
    # starts after completion so a slow fetch still gives queued commands breathing room.
    cache = request.app.state.map_cache
    async with request.app.state.map_lock:
        ttl = _MAP_RETRY_AFTER_FAILURE_SEC if cache.get("stale") else _MAP_TTL_SEC
        if "stale" not in cache or time.monotonic() - cache["at"] >= ttl:
            gateway = _gw(request)
            cache["png"] = await gateway.get_map_png()
            cache["stale"] = not gateway.map_ok()
            cache["at"] = time.monotonic()
        headers = {"Cache-Control": "no-store", "X-Map-Stale": str(cache["stale"]).lower()}
        if not cache["png"]:
            return Response(status_code=503, headers=headers)
        return Response(content=cache["png"], media_type="image/png", headers=headers)


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


@app.exception_handler(SettingsNotConfirmed)
async def settings_not_confirmed(request: Request, exc: SettingsNotConfirmed):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _carpet_selection(value: str | None) -> CarpetMode | None:
    if value is None:
        return None
    mode = _parse(CarpetMode, value)
    if mode not in QREVO_L_PRO.carpet_modes:
        raise HTTPException(422, "Carpet mode is not supported by this app for this robot")
    return mode


@asynccontextmanager
async def _control_slot(request: Request, *, starting: bool = False):
    slot = request.app.state.run_slot
    if not slot.acquire():
        raise HTTPException(409, "another run or settings change is already in progress")
    try:
        if starting and not ready_to_start(await _gw(request).get_status()):
            raise HTTPException(409, "the robot is not ready: it must be docked, idle and error-free")
        yield
    finally:
        slot.release()


@app.post("/start")
async def start(
    request: Request, mode: str = Body(..., embed=True),
    carpet_mode: str | None = Body(None, embed=True),
):
    async with _control_slot(request, starting=True):
        await start_clean(
            _gw(request),
            cleaning_log=request.app.state.cleaning_log,
            source=CleanSource.APP,
            mode=_parse(CleaningMode, mode),
            segment_ids=None,
            carpet_mode=_carpet_selection(carpet_mode),
            now=request.app.state.now,
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


# Stop, Dock and both Cancels put Up next on hold: the user is sending the robot home,
# and a queued Clean must not send it straight back out.
@app.post("/stop")
async def stop(request: Request):
    request.app.state.clean_queue.hold(STOPPED_BY_USER)
    await _gw(request).stop()
    return {"ok": True}


@app.post("/dock")
async def dock(request: Request):
    request.app.state.clean_queue.hold(STOPPED_BY_USER)
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
    async with _control_slot(request):
        await _gw(request).set_fan_power(_parse(FanPower, power))
    return {"ok": True}


@app.post("/mop")
async def mop(request: Request, mode: str = Body(..., embed=True)):
    async with _control_slot(request):
        await _gw(request).set_mop_mode(_parse(MopMode, mode))
    return {"ok": True}


@app.post("/water")
async def water(request: Request, mode: str = Body(..., embed=True)):
    async with _control_slot(request):
        await _gw(request).set_water_mode(_parse(WaterMode, mode))
    return {"ok": True}


@app.get("/carpet")
async def carpet_get(request: Request):
    # One boot-time read so the Setup carpet toggle can show the device's current setting;
    # deliberately NOT part of the status poll (it costs a device command each call).
    try:
        mode = await _gw(request).get_carpet_mode()
    except Exception:
        _LOGGER.warning("carpet mode read failed", exc_info=True)
        raise HTTPException(503, "Carpet mode temporarily unavailable")
    return {"mode": mode.value}


@app.post("/carpet")
async def carpet(request: Request, mode: str = Body(..., embed=True)):
    parsed = _parse(CarpetMode, mode)
    if parsed is CarpetMode.UNKNOWN:
        raise HTTPException(422, "cannot set carpet mode to 'unknown'")
    async with _control_slot(request):
        gw = _gw(request)
        await gw.set_carpet_mode(parsed)
        reported = await gw.get_carpet_mode()
        if reported is not parsed:
            raise SettingsNotConfirmed(
                f"Carpet mode not confirmed: requested {parsed.value}, robot reported {reported.value}."
            )
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
    carpet_mode: str | None = Body(None, embed=True),
):
    if not segment_ids:
        raise HTTPException(422, "segment_ids must not be empty")
    if not 1 <= repeat <= 3:
        raise HTTPException(422, "repeat must be between 1 and 3")
    async with _control_slot(request, starting=True):
        await start_clean(
            _gw(request),
            cleaning_log=request.app.state.cleaning_log,
            source=CleanSource.APP,
            mode=_parse(CleaningMode, mode),
            segment_ids=segment_ids,
            repeat=repeat,
            carpet_mode=_carpet_selection(carpet_mode),
            now=request.app.state.now,
        )
    return {"ok": True}


_DAY = timedelta(days=1)


def _interval_body(interval: RoomInterval) -> dict:
    return {"segment_id": interval.segment_id, "interval_days": interval.interval / _DAY}


@app.get("/intervals")
async def intervals_list(request: Request):
    return [_interval_body(i) for i in await request.app.state.room_intervals.list_intervals()]


@app.post("/intervals")
async def intervals_set(
    request: Request,
    segment_id: int = Body(..., embed=True),
    interval_days: float = Body(..., embed=True),
):
    try:
        interval = RoomInterval(segment_id=segment_id, interval=timedelta(days=interval_days))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await request.app.state.room_intervals.set_interval(interval)
    return {"ok": True}


@app.delete("/intervals/{segment_id}")
async def intervals_remove(request: Request, segment_id: int):
    if not await request.app.state.room_intervals.remove_interval(segment_id):
        raise HTTPException(404, f"segment {segment_id} has no Room Interval")
    return {"ok": True}


def _overdue_body(entry: SegmentOverdue) -> dict:
    return {
        "segment_id": entry.segment_id,
        "interval_days": entry.interval / _DAY,
        "last_cleaned_at": None if entry.never_cleaned else entry.last_cleaned_at.isoformat(),
        # Null Priority means "maximally overdue", which JSON cannot spell; never_cleaned is
        # what stops it reading as missing data.
        "priority": entry.priority,
        "never_cleaned": entry.never_cleaned,
        "overdue": entry.overdue,
    }


@app.get("/overdue")
async def overdue(request: Request):
    report = await overdue_view(
        intervals=request.app.state.room_intervals,
        cleaning_log=request.app.state.cleaning_log,
        now=request.app.state.now,
    )
    return [_overdue_body(entry) for entry in report]


@app.post("/clean/overdue")
async def clean_overdue_now(request: Request, mode: str = Body(..., embed=True)):
    # Two distinct mechanisms, so two distinct conflicts: the in-process RunSlot, then a
    # live status read. The slot is held across both so no run starts in between.
    slot = request.app.state.run_slot
    if not slot.acquire():
        raise HTTPException(409, "another run is already in progress")
    try:
        gw = _gw(request)
        if not ready_to_start(await gw.get_status()):
            raise HTTPException(
                409, "the robot is not ready: it must be docked, idle and error-free"
            )
        cleaned = await clean_overdue(
            gw,
            intervals=request.app.state.room_intervals,
            cleaning_log=request.app.state.cleaning_log,
            source=CleanSource.APP,
            mode=_parse(CleaningMode, mode),
            now=request.app.state.now,
        )
    finally:
        slot.release()
    return {"ok": True, "segment_ids": cleaned}


def _parse_recurrence(body: dict) -> WeeklyGrid:
    days = body.get("days")
    if not isinstance(days, list):
        raise HTTPException(422, "recurrence.days must be a list of weekday names")
    raw_at = body.get("at")
    try:
        at = time_of_day.fromisoformat(raw_at)
    except (TypeError, ValueError):
        raise HTTPException(422, f"invalid time {raw_at!r} for recurrence.at: expected HH:MM")
    # fromisoformat is laxer than the HH:MM contract: it accepts seconds, compact "0930",
    # and — worst — a UTC offset that generation would discard while the response echoed
    # it back. An offset survives the canonical round-trip, so it needs its own check.
    if at.tzinfo is not None or raw_at != at.isoformat(timespec="minutes"):
        raise HTTPException(422, f"invalid time {raw_at!r} for recurrence.at: expected HH:MM")
    try:
        return WeeklyGrid(days=frozenset(_parse(Weekday, day) for day in days), at=at)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


async def _parse_action(request: Request, body: dict) -> CleanAction | SequenceAction:
    if body.get("type") == "sequence":
        sequence_id = body.get("sequence_id")
        if not isinstance(sequence_id, int) or isinstance(sequence_id, bool):
            raise HTTPException(422, "action.sequence_id must be a Sequence id")
        if await request.app.state.sequences.get_sequence(sequence_id) is None:
            raise HTTPException(422, f"no Sequence {sequence_id}")
        return SequenceAction(sequence_id=sequence_id)
    if body.get("type") != "clean":
        raise HTTPException(
            422, f"invalid action type {body.get('type')!r}: expected 'clean' or 'sequence'"
        )
    segment_ids = body.get("segment_ids")
    if segment_ids is not None and (
        not isinstance(segment_ids, list) or not all(isinstance(s, int) for s in segment_ids)
    ):
        raise HTTPException(422, "action.segment_ids must be a list of Segment numbers or null")
    try:
        return CleanAction(
            mode=_parse(CleaningMode, body.get("mode")),
            segment_ids=None if segment_ids is None else tuple(segment_ids),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _action_body(action: CleanAction | SequenceAction) -> dict:
    if isinstance(action, SequenceAction):
        return {"type": "sequence", "sequence_id": action.sequence_id}
    segment_ids = action.segment_ids
    return {
        "type": "clean",
        "mode": action.mode.value,
        "segment_ids": None if segment_ids is None else list(segment_ids),
    }


def _rule_body(rule: ScheduleRule, tz: ZoneInfo, now) -> dict:
    # "Next" looks exactly one Materialisation horizon ahead, so it can never disagree
    # with what gets materialised.
    upcoming = (
        rule.recurrence.occurrences_between(now, now + MATERIALISATION_HORIZON, tz)
        if rule.enabled
        else []
    )
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "recurrence": {
            "days": [day.value for day in Weekday if day in rule.recurrence.days],
            "at": rule.recurrence.at.isoformat(timespec="minutes"),
        },
        "action": _action_body(rule.action),
        "next_fires_at": upcoming[0].isoformat() if upcoming else None,
    }


async def _materialise(request: Request) -> None:
    await materialise(
        request.app.state.schedule_rules,
        request.app.state.occurrences,
        now=request.app.state.now(),
        tz=request.app.state.timezone,
    )


@app.get("/schedules")
async def schedules_list(request: Request):
    tz, now = request.app.state.timezone, request.app.state.now()
    rules = await request.app.state.schedule_rules.list_rules()
    return [_rule_body(rule, tz, now) for rule in rules]


@app.post("/schedules")
async def schedules_create(
    request: Request,
    name: str = Body(..., embed=True),
    recurrence: dict = Body(..., embed=True),
    action: dict = Body(..., embed=True),
    enabled: bool = Body(True, embed=True),
):
    rule = await request.app.state.schedule_rules.create_rule(
        name=name,
        recurrence=_parse_recurrence(recurrence),
        action=await _parse_action(request, action),
        enabled=enabled,
    )
    await _materialise(request)
    return _rule_body(rule, request.app.state.timezone, request.app.state.now())


@app.put("/schedules/{rule_id}")
async def schedules_replace(
    request: Request,
    rule_id: int,
    name: str = Body(..., embed=True),
    recurrence: dict = Body(..., embed=True),
    action: dict = Body(..., embed=True),
    enabled: bool = Body(True, embed=True),
):
    rule = ScheduleRule(
        id=rule_id,
        name=name,
        recurrence=_parse_recurrence(recurrence),
        action=await _parse_action(request, action),
        enabled=enabled,
    )
    if not await request.app.state.schedule_rules.replace_rule(rule):
        raise HTTPException(404, f"no Schedule Rule {rule_id}")
    # A restated intent supersedes the old one: cancel every outstanding PENDING
    # Occurrence — a CLAIMED one rides it out — then re-materialise.
    await request.app.state.occurrences.cancel_pending(rule_id)
    await _materialise(request)
    return _rule_body(rule, request.app.state.timezone, request.app.state.now())


@app.delete("/schedules/{rule_id}")
async def schedules_delete(request: Request, rule_id: int):
    if not await request.app.state.schedule_rules.delete_rule(rule_id):
        raise HTTPException(404, f"no Schedule Rule {rule_id}")
    await request.app.state.occurrences.cancel_pending(rule_id)
    return {"ok": True}


def _occurrence_body(occurrence: Occurrence) -> dict:
    return {
        "id": occurrence.id,
        "rule_id": occurrence.rule_id,
        "rule_name": occurrence.rule_name,
        "scheduled_for": occurrence.scheduled_for.isoformat(),
        "deadline": occurrence.deadline.isoformat(),
        "state": occurrence.state.value,
        "attempts": occurrence.attempts,
        "blocked_reason": occurrence.blocked_reason,
        "fired_at": None if occurrence.fired_at is None else occurrence.fired_at.isoformat(),
    }


@app.get("/occurrences")
async def occurrences_timeline(request: Request):
    """The timeline: every past and upcoming Occurrence, earliest scheduled first."""
    timeline = await request.app.state.occurrences.list_occurrences()
    return [_occurrence_body(occurrence) for occurrence in timeline]


@app.get("/scheduler/status")
async def scheduler_status(request: Request):
    """The scheduler health summary, including the resolved timezone — the setting most
    likely to be silently wrong."""
    scheduler = request.app.state.scheduler
    pending = sorted(
        occurrence.scheduled_for
        for occurrence in await request.app.state.occurrences.list_occurrences()
        if occurrence.state is OccurrenceState.PENDING
    )
    return {
        "last_tick_at": (
            None if scheduler.last_tick_at is None else scheduler.last_tick_at.isoformat()
        ),
        "consecutive_errors": scheduler.consecutive_errors,
        "current_backoff_seconds": scheduler.backoff_sec(),
        "pending_count": len(pending),
        "next_scheduled_at": pending[0].isoformat() if pending else None,
        "timezone": request.app.state.config.timezone,
    }


@app.post("/clean/two-stage")
async def clean_two_stage(request: Request, segment_ids: list[int] = Body(None, embed=True)):
    slot = request.app.state.run_slot
    if not slot.acquire():
        raise HTTPException(409, "another run is already in progress")
    gw = _gw(request)
    config = request.app.state.config
    progress = slot.progress
    # Cleared, not updated: the slot's last run may have been a Sequence, whose extra
    # keys must not leak into this endpoint's status shape.
    progress.clear()
    progress.update(stage="vacuuming", message="starting", error=None)

    def on_stage(stage: str) -> None:
        progress["stage"] = stage
        progress["message"] = _STAGE_MESSAGES.get(stage, "")

    async def runner() -> None:
        try:
            await run_two_stage(
                gw, config,
                cleaning_log=request.app.state.cleaning_log, source=CleanSource.APP,
                segment_ids=segment_ids or None, on_stage=on_stage,
                log=_LOGGER.info, cancel=slot.cancel,
            )
        except RunCancelled:
            # The use case has already docked the robot and restored carpet mode.
            progress.update(stage="cancelled", message="Two-stage clean cancelled", error=None)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the status endpoint
            progress.update(stage="error", message=str(exc), error=str(exc))
        finally:
            slot.release()

    slot.task = asyncio.create_task(runner())
    return {"ok": True}


@app.post("/clean/two-stage/cancel")
async def clean_two_stage_cancel(request: Request):
    slot = request.app.state.run_slot
    if not slot.running:
        raise HTTPException(409, "no two-stage clean is running")
    request.app.state.clean_queue.hold(STOPPED_BY_USER)
    slot.cancel.set()
    return {"ok": True}


@app.get("/clean/two-stage/status")
async def clean_two_stage_status(request: Request):
    slot = request.app.state.run_slot
    return {"running": slot.running, **slot.progress}


def _parse_optional(enum_cls, value):
    """A Step setting: null is "Same as Setup"."""
    return None if value is None else _parse(enum_cls, value)


def _parse_step(body) -> Step:
    if not isinstance(body, dict):
        raise HTTPException(422, "steps must be a list of Steps")
    segment_ids = body.get("segment_ids")
    if segment_ids is not None and (
        not isinstance(segment_ids, list) or not all(isinstance(s, int) for s in segment_ids)
    ):
        raise HTTPException(422, "a Step's segment_ids must be a list of Segment numbers or null")
    repeat = body.get("repeat", 1)
    if not isinstance(repeat, int) or isinstance(repeat, bool):
        raise HTTPException(422, "a Step's repeat must be a whole number")
    try:
        return Step(
            mode=_parse(CleaningMode, body.get("mode")),
            carpet=_parse(CarpetMode, body.get("carpet")),
            segment_ids=None if segment_ids is None else tuple(segment_ids),
            repeat=repeat,
            settings=CleanSettings(
                fan_power=_parse_optional(FanPower, body.get("fan_power")),
                water_mode=_parse_optional(WaterMode, body.get("water_mode")),
                mop_route=_parse_optional(MopMode, body.get("mop_route")),
            ),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _parse_sequence(sequence_id: int, name, steps) -> Sequence:
    if not isinstance(name, str):
        raise HTTPException(422, "a Sequence needs a name")
    if not isinstance(steps, list):
        raise HTTPException(422, "steps must be a list of Steps")
    try:
        return Sequence(id=sequence_id, name=name, steps=tuple(_parse_step(s) for s in steps))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _sequence_body(sequence: Sequence) -> dict:
    return {
        "id": sequence.id,
        "name": sequence.name,
        "steps": [step_body(step) for step in sequence.steps],
    }


async def _sequence_or_404(request: Request, sequence_id: int) -> Sequence:
    sequence = await request.app.state.sequences.get_sequence(sequence_id)
    if sequence is None:
        raise HTTPException(404, f"no Sequence {sequence_id}")
    return sequence


# The fixed paths are declared before /sequences/{sequence_id} so they are not read as ids.
@app.get("/sequences/progress")
async def sequences_progress(request: Request):
    slot = request.app.state.run_slot
    return {"running": slot.running, **slot.progress}


@app.post("/sequences/cancel")
async def sequences_cancel(request: Request):
    slot = request.app.state.run_slot
    if not slot.running:
        raise HTTPException(409, "nothing is running")
    request.app.state.clean_queue.hold(STOPPED_BY_USER)
    slot.cancel.set()
    return {"ok": True}


@app.get("/sequences")
async def sequences_list(request: Request):
    return [_sequence_body(s) for s in await request.app.state.sequences.list_sequences()]


@app.post("/sequences")
async def sequences_create(
    request: Request,
    name: object = Body(..., embed=True),
    steps: object = Body(..., embed=True),
):
    # Validated through the entity before an id exists; the store assigns the real one.
    draft = _parse_sequence(0, name, steps)
    sequence = await request.app.state.sequences.create_sequence(draft.name, draft.steps)
    return _sequence_body(sequence)


@app.get("/sequences/{sequence_id}")
async def sequences_read(request: Request, sequence_id: int):
    return _sequence_body(await _sequence_or_404(request, sequence_id))


@app.put("/sequences/{sequence_id}")
async def sequences_replace(
    request: Request,
    sequence_id: int,
    name: object = Body(..., embed=True),
    steps: object = Body(..., embed=True),
):
    sequence = _parse_sequence(sequence_id, name, steps)
    if not await request.app.state.sequences.replace_sequence(sequence):
        raise HTTPException(404, f"no Sequence {sequence_id}")
    return _sequence_body(sequence)


@app.delete("/sequences/{sequence_id}")
async def sequences_delete(request: Request, sequence_id: int):
    users = [
        rule.name
        for rule in await request.app.state.schedule_rules.list_rules()
        if rule.action == SequenceAction(sequence_id=sequence_id)
    ]
    if users:
        names = ", ".join(repr(name) for name in users)
        which = "that rule" if len(users) == 1 else "those rules"
        raise HTTPException(
            409, f"Schedule Rule {names} runs this Sequence. Change or delete {which} first."
        )
    if not await request.app.state.sequences.delete_sequence(sequence_id):
        raise HTTPException(404, f"no Sequence {sequence_id}")
    return {"ok": True}


@app.post("/sequences/{sequence_id}/run")
async def sequences_run(request: Request, sequence_id: int):
    sequence = await _sequence_or_404(request, sequence_id)
    slot = request.app.state.run_slot
    if not slot.acquire():
        raise HTTPException(409, "another run is already in progress")
    # Held across the readiness check and the launch, so nothing can start in between.
    try:
        reason = readiness_block(await _gw(request).get_status(), run_in_flight=False)
    except BaseException:
        slot.release()
        raise
    if reason is not None:
        slot.release()
        raise HTTPException(409, reason)
    request.app.state.sequence_launcher.launch(
        sequence.steps, name=sequence.name, sequence_id=sequence.id, source=CleanSource.APP
    )
    return {"ok": True}


def _queue_entry_body(entry: QueuedClean) -> dict:
    return {
        "id": entry.id,
        "label": entry.label,
        "sequence_id": entry.sequence_id,
        "steps": [step_body(step) for step in entry.steps],
    }


def _queue_body(queue: CleanQueue) -> dict:
    return {
        "held": queue.held,
        "hold_reason": queue.hold_reason,
        "waiting_for": queue.waiting_for,
        "entries": [_queue_entry_body(entry) for entry in queue.entries],
    }


@app.get("/queue")
async def queue_read(request: Request):
    return _queue_body(request.app.state.clean_queue)


@app.post("/queue")
async def queue_add(
    request: Request,
    label: object = Body(None, embed=True),
    steps: object = Body(None, embed=True),
    sequence_id: object = Body(None, embed=True),
    preset: object = Body(None, embed=True),
    segment_ids: object = Body(None, embed=True),
):
    """Add a Clean to Up next: `steps` under a `label`, the Two-Stage `preset` over
    `segment_ids`, or a saved Sequence's Steps by `sequence_id`. Steps are copied, so
    editing the Sequence later leaves the queued entry as it was requested."""
    if preset is not None:
        if preset != "two-stage":
            raise HTTPException(422, "the only preset is 'two-stage'")
        if not isinstance(label, str):
            raise HTTPException(422, "a queued Clean needs a label")
        if segment_ids is not None and (
            not isinstance(segment_ids, list) or not all(isinstance(s, int) for s in segment_ids)
        ):
            raise HTTPException(422, "segment_ids must be a list of Segment numbers or null")
        parsed, origin = two_stage_steps(segment_ids), None
    elif sequence_id is not None:
        if not isinstance(sequence_id, int) or isinstance(sequence_id, bool):
            raise HTTPException(422, "sequence_id must be a Sequence number")
        sequence = await _sequence_or_404(request, sequence_id)
        label, parsed, origin = sequence.name, sequence.steps, sequence.id
    else:
        if not isinstance(label, str):
            raise HTTPException(422, "a queued Clean needs a label")
        if not isinstance(steps, list):
            raise HTTPException(422, "steps must be a list of Steps")
        parsed, origin = tuple(_parse_step(step) for step in steps), None
    try:
        entry = request.app.state.clean_queue.add(label, parsed, sequence_id=origin)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return _queue_entry_body(entry)


@app.post("/queue/resume")
async def queue_resume(request: Request):
    request.app.state.clean_queue.resume()
    return _queue_body(request.app.state.clean_queue)


@app.post("/queue/clear")
async def queue_clear(request: Request):
    request.app.state.clean_queue.clear()
    return _queue_body(request.app.state.clean_queue)


@app.delete("/queue/{entry_id}")
async def queue_remove(request: Request, entry_id: int):
    if not request.app.state.clean_queue.remove(entry_id):
        raise HTTPException(404, f"nothing queued as {entry_id}")
    return _queue_body(request.app.state.clean_queue)


@app.post("/queue/{entry_id}/move")
async def queue_move(request: Request, entry_id: int, to: int = Body(..., embed=True)):
    if not request.app.state.clean_queue.move(entry_id, to):
        raise HTTPException(404, f"nothing queued as {entry_id}")
    return _queue_body(request.app.state.clean_queue)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker():
    # Served from the ROOT, not the static mount: a service worker only controls pages at
    # or below its own path, so /static/sw.js could never control /. The header widens the
    # allowed scope explicitly as belt-and-braces.
    version = hashlib.sha256((STATIC_DIR / "index.html").read_bytes()).hexdigest()[:16]
    worker = (STATIC_DIR / "sw.js").read_text().replace("__SHELL_VERSION__", version)
    return Response(
        worker,
        media_type="text/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
