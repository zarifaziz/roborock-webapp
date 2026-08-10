# Implementation Plan: Roborock local control web app (panel + live map)

Companion to `PLAN.md`. This covers the interactive control app. The scheduled
carpet-aware two-stage clean from `PLAN.md` stays as the unattended path (GitHub Actions
cron) because it must fire when nobody is present. Both share one core module.

## Decisions locked

- Scope v1: **control panel + live map**. Status, start/pause/dock, room clean by name,
  fan power, mop mode, carpet toggle, trigger saved routines, consumables — plus the
  rendered map with robot position and room highlights. No interactive map editing yet.
- Hosting: **local, on-demand**. You launch it when you want to drive the robot; it is not
  an always-on server. No hosting cost. Live control only while it is running.
- Because it is on-demand, the unattended two-stage clean does NOT live here — that remains
  the cron in `PLAN.md`. This app is the human-in-the-loop surface.

## How the pieces relate

```
 shared core (roborock_auto.core)
   session load  ·  DeviceManager  ·  RoborockApiClient  ·  command helpers
        ├──────────────► PLAN.md cron (run.py)      unattended two-stage clean
        └──────────────► this web app (backend)     interactive panel + map

 web app (only while you have it open):
   Browser SPA  ──WebSocket(live)──►  FastAPI backend  ──cloud──►  Qrevo L Pro
                ──REST(commands)──►      poll loop (~5s): status + map refresh
```

## Confirmed library surface (already verified)

- Control via `device.v1_properties.command.send(RoborockCommand.X, params=...)`:
  `APP_START`, `APP_PAUSE`, `APP_CHARGE` (dock), `APP_SEGMENT_CLEAN` (rooms),
  `APP_ZONED_CLEAN`, `APP_GOTO_TARGET`, fan power, mop mode, `SET_CARPET_CLEAN_MODE`.
- Status: `device.v1_properties.status.refresh()` then `.state`, `.battery`, dock type,
  error codes. Poll-only (no push at this API level), so the backend polls.
- Rooms: `device.v1_properties.rooms` for segment id <-> name.
- Consumables: `device.v1_properties.consumables` (brush/filter life).
- Map: `device.v1_properties.map_content.refresh()` gives `.image_content` (rendered PNG
  bytes) and `.map_data` (`ParsedMapData`: rooms, robot position, zones). This is why the
  live map is feasible without hand-rolling a map parser.
- Routines: `RoborockApiClient.get_scenes(user_data, duid)` /
  `execute_scene(user_data, scene_id)`.

## Repo layout (extends the PLAN.md layout)

```
src/roborock_auto/
  core.py          # NEW shared: load session, build DeviceManager + web client, close
  login.py         # unchanged (interactive one-time login -> user_data.json)
  discover.py      # unchanged (read-only dump)
  run.py           # PLAN.md cron; refactored to import core
  webapp/
    __init__.py
    server.py      # FastAPI app: REST command endpoints + WebSocket + static mount
    poller.py      # background task: refresh status + map every ~5s, broadcast diffs
    commands.py    # thin wrappers mapping REST calls to command.send(...)
    static/        # built frontend (index.html, assets)
  frontend/        # Vite + React source (dev); builds into webapp/static
pyproject.toml     # add: fastapi, uvicorn, websockets; dev: the frontend toolchain separate
```

Frontend: Vite + React, because the room picker and map overlay want real interactivity.
(No-build fallback: a single static `index.html` with vanilla JS + WebSocket, if you'd
rather skip a JS build step — the map is just an `<img>` the backend serves.)

Launch: `uv run uvicorn roborock_auto.webapp.server:app` opens `http://localhost:8000`.

## Backend design

- On startup: load `user_data.json` via `core`, build `DeviceManager`, resolve the Qrevo
  `duid`, keep the connection warm for the process lifetime.
- Poll loop (`poller.py`): every ~5s call `status.refresh()` and `map_content.refresh()`.
  Diff against last snapshot; broadcast changed fields as JSON over WebSocket. Serve the
  map PNG at `GET /map.png` (or push a version tag over WS and let the browser refetch).
- REST endpoints, each a thin call into `commands.py`:
  - `POST /start` `/pause` `/dock`
  - `POST /clean/rooms` body `{segment_ids: [...]}` -> `APP_SEGMENT_CLEAN`
  - `POST /fan` `{level}` · `POST /mop` `{mode}`
  - `POST /carpet` `{mode}` -> `SET_CARPET_CLEAN_MODE`
  - `POST /routine` `{scene_id}` -> `execute_scene`
  - `GET /rooms` · `GET /routines` · `GET /consumables`
- Every device/cloud call wrapped in the same small retry helper as the cron path.
- Single-user, localhost only, so no auth. Bind to 127.0.0.1 so it is never exposed.

## Frontend design

- One page. Top: status card (battery, state text, dock, errors, current map name).
- Center: live map `<img>` with a lightweight overlay layer for robot position and room
  highlights read from `map_data`. Rooms clickable to select for a segment clean.
- Controls: start / pause / dock buttons; fan-power and mop-mode selectors; carpet-mode
  toggle (Rise / Avoid / others found in discovery); routine buttons from `/routines`;
  consumables read-out.
- WebSocket client updates the status card and map live; command buttons POST then let the
  next poll reflect the new state.

## Build order

1. Extract `core.py` from the existing scripts (login/discover/run import it). Verify:
   discover still runs unchanged.
2. Backend skeleton: FastAPI up, session loaded, `GET /status` returns one live snapshot.
   Verify: curl shows real battery/state.
3. Poll loop + WebSocket: browser test page shows status updating live. Verify: numbers
   move when you poke the robot in the real app.
4. Map endpoint: `GET /map.png` serves the rendered PNG. Verify: image loads in browser.
5. Command endpoints one at a time, safest first: dock, then start/pause, then
   segment clean, fan/mop, carpet, routines. Verify each against the real robot.
6. Frontend: status card -> map -> controls, wired to the endpoints and WebSocket.
7. Overlay: robot position + clickable rooms from `map_data`.

## Deferred (explicitly not in v1)

- Interactive map editing (draw no-go zones / custom zones on the map), schedules editor,
  multi-map management. These need less-proven map-write commands; add later if wanted.
- Remote access / auth / TLS — out of scope while it is localhost on-demand.

## Risks specific to the app

- Poll interval is a tradeoff: too tight hammers the cloud API, too loose feels laggy.
  Start ~5s, tune. Consider pausing the poll loop when no browser is connected.
- Map render cost and payload size — only refetch the PNG when `map_data` actually changes,
  not every poll.
- Same session-expiry risk as the cron: on auth failure, rerun `login.py`.
