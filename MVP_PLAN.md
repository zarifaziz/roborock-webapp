# MVP Plan: Roborock local control app + carpet automation

Consolidates `PLAN.md` (unattended carpet-aware clean) and `WEBAPP_PLAN.md` (local control
app with live map) into one buildable MVP. Context and ruled-out alternatives live in
`handoff.md`.

## The core idea that makes this buildable without account access

Everything routes through one abstraction, a **Gateway**. Two implementations:

- `FakeGateway`: canned status, a sample map PNG, fake rooms/routines/consumables. No
  network, no account. The whole app and both scripts run against this today.
- `LiveGateway`: the real `python-roborock` (`DeviceManager` + `RoborockApiClient`).
  Dropped in once the user logs in.

The MVP is "ready to test" when the app runs fully against `FakeGateway` on the laptop,
tests pass, and swapping to `LiveGateway` is only filling in a `.env` file (credentials +
discovered ids). We build up to exactly the point where real account data is the only thing
missing.

```
 Browser (static HTML+JS) ──REST poll──► FastAPI ──► Gateway (interface)
                                                       ├─ FakeGateway  (now: canned + sample map)
                                                       └─ LiveGateway  (later: python-roborock)
 cron run.py ────────────────────────────────────────► same Gateway
```

## Definition of done (MVP)

1. `uv run` launches the app on `http://127.0.0.1:8000`, serving a static single page.
2. Against `FakeGateway`: status card shows battery/state and refreshes every ~5s via a
   `setInterval` fetch of `/status`; map image renders; buttons (start/pause/dock, room
   clean, fan, mop, carpet, routine) POST and the fake state responds visibly.
3. `login.py`, `discover.py`, `run.py` exist, import the shared core, and run against
   `FakeGateway` end to end (login is a no-op in fake mode; discover prints fake data;
   run.py executes the full two-stage sequence with the fake completion poll).
4. `pytest` green: gateway contract test, command mapping, the two-stage sequence, the
   completion poll (depart-then-return incl. recharge-resume and error cases), API endpoints
   via FastAPI TestClient, and LiveGateway's command/param mapping via mocks.
5. Going live requires only editing `.env` (credentials + discovered duid, scene ids, carpet
   values) and setting `GATEWAY=live`; no code change. `LiveGateway` is written against the
   confirmed API even though it can't be exercised without an account.
6. `.github/workflows/clean.yml` present (unattended cron path), not enabled to run without
   secrets.

## Confirmed library API (verified against python-roborock source)

- Login: `RoborockApiClient(username=email)`, `await request_code_v4()`,
  `await code_login_v4(code) -> UserData`. Persist `user_data.as_dict()` to JSON, reload
  with `UserData.from_dict(...)`. Import: `from roborock.web_api import RoborockApiClient`,
  `from roborock.data.containers import UserData`.
- Devices: `UserParams(username, user_data)` ->
  `await create_device_manager(user_params)` -> `await get_devices()` -> `.name`, `.duid`,
  `.v1_properties`. Always `await device_manager.close()`.
- Status: `device.v1_properties.status.refresh()` then `.state` (`RoborockStateCode`),
  `.battery`, dock type, error. Poll-only.
- Command: `await device.v1_properties.command.send(RoborockCommand.X, params=...)`.
  Uses: `APP_START`, `APP_PAUSE`, `APP_CHARGE`, `APP_SEGMENT_CLEAN`, `APP_ZONED_CLEAN`,
  fan power, mop mode, `SET_CARPET_CLEAN_MODE` / `GET_CARPET_CLEAN_MODE`.
- Rooms: `device.v1_properties.rooms`. Consumables: `.consumables`.
- Map: `device.v1_properties.map_content.refresh()` -> `.image_content` (PNG bytes),
  `.map_data` (`ParsedMapData`: rooms, robot position, zones).
- Scenes/routines: `RoborockApiClient.get_scenes(user_data, duid)` /
  `execute_scene(user_data, scene_id)`.

Undiscoverable until a real device is connected. These are NOT hardcoded in code — they
live in `.env` / `config.py`, filled after discovery: carpet-mode integer values
(Rise/Avoid), the user's `duid`, and the vacuum/mop `scene_id`s. The `RoborockStateCode`
-> semantic-name mapping is a static table inside `LiveGateway` (state codes are library
constants, not per-user), refined once we see real values.

## Live-mode config surface (config.py, read from .env)

`GATEWAY` (`fake`|`live`, default `fake`), `ROBOROCK_EMAIL`, `ROBOROCK_DUID`,
`VACUUM_SCENE_ID`, `MOP_SCENE_ID`, `CARPET_RISE_VALUE`, `CARPET_AVOID_VALUE`,
`POLL_INTERVAL_SEC` (default 5), `APP_PORT` (default 8000). In fake mode every account field
has a working default so nothing must be set. `automation.py` reads scene ids and carpet
values from config, so it is identical in fake and live mode.

## The Gateway interface

A single Protocol both scripts and the app depend on. Async methods, semantic values in and
plain dataclasses out — no python-roborock types and no firmware magic integers leak past
the boundary. Each gateway owns its own int mapping (LiveGateway from config/state table):

```
class CarpetMode(Enum): RISE, AVOID          # gateway maps to config ints
class FanPower(Enum):   QUIET, BALANCED, TURBO, MAX
class MopMode(Enum):    STANDARD, DEEP

class Gateway(Protocol):
    async def connect(self) -> None
    async def close(self) -> None
    async def get_status(self) -> DeviceStatus        # battery, state_name, in_cleaning, docked, error
    async def get_map_png(self) -> bytes              # rendered map image
    async def get_map_data(self) -> MapData           # rooms[], robot xy, zones
    async def list_rooms(self) -> list[Room]
    async def list_routines(self) -> list[Routine]    # Routine has id + name
    async def get_consumables(self) -> Consumables
    async def start(self) -> None
    async def pause(self) -> None
    async def dock(self) -> None
    async def clean_rooms(self, segment_ids: list[int]) -> None   # segment ids come from list_rooms()
    async def set_fan_power(self, power: FanPower) -> None
    async def set_mop_mode(self, mode: MopMode) -> None
    async def set_carpet_mode(self, mode: CarpetMode) -> None
    async def get_carpet_mode(self) -> CarpetMode
    async def run_routine(self, routine_id: int) -> None          # id from list_routines()
```

`DeviceStatus` fields: `state_name` (normalized string: "cleaning", "charging", "idle",
"error", ...), `in_cleaning` (bool — true while a job is active even if temporarily docked
to recharge), `docked` (bool), `battery` (int), `error` (str|None). The completion poll and
UI depend only on these, never on raw integers. `LiveGateway` maps `RoborockStateCode` and
the status `in_cleaning`/`in_returning` flags into these; `FakeGateway` sets them directly.

## Repo layout

```
roborock-automation/
  pyproject.toml            # uv, python 3.12 pin; deps: python-roborock, fastapi, uvicorn
  .python-version           # 3.12
  .gitignore                # user_data.json, .env, __pycache__, node_modules, dist
  README.md
  handoff.md PLAN.md WEBAPP_PLAN.md MVP_PLAN.md
  src/roborock_auto/
    __init__.py
    config.py               # env: GATEWAY (fake|live), email, poll interval, port
    models.py               # DeviceStatus, MapData, Room, Routine, Consumables dataclasses
    gateway/
      __init__.py           # get_gateway() factory reading config.GATEWAY
      base.py               # Gateway Protocol
      fake.py               # FakeGateway (+ bundled sample_map.png, fake state machine)
      live.py               # LiveGateway (python-roborock)
    core.py                 # session load/save, build LiveGateway pieces, retry helper
    login.py                # interactive one-time login -> user_data.json
    discover.py             # read-only dump via gateway
    automation.py           # two-stage clean sequence + depart-then-return poll (pure-ish)
    run.py                  # cron entrypoint: build gateway, run automation
    webapp/
      server.py             # FastAPI: REST endpoints + static mount (no websocket)
      static/index.html     # single page: vanilla HTML + JS, no build step
  tests/
    test_gateway_fake.py    # fake conforms to contract
    test_automation.py      # two-stage sequence + poll (recharge-resume, error, precondition)
    test_api.py             # endpoints via TestClient against fake
    test_live_mapping.py    # LiveGateway command/param + state-code mapping via mocks
  .github/workflows/clean.yml
```

## Build phases (each ends with a green check)

**P0 Scaffold + repo.** git init (done), `.gitignore`, `pyproject.toml`, uv env on 3.12,
`python-roborock` + fastapi install resolves. Verify: `uv run python -c "import roborock"`.

**P1 Models + Gateway interface + FakeGateway.** Dataclasses (with `in_cleaning`/`error`),
Protocol, semantic enums, and a FakeGateway driven by an **injectable clock**: transitions
are functions of elapsed time since `start()` (docked -> cleaning after a few seconds ->
charging when "done"), so observation never mutates progress. Scriptable so a test can force
recharge-resume and error states. Bundled sample map PNG. Verify: `test_gateway_fake.py`
green; calling `get_status()` twice without advancing the clock returns the same state.

**P2 Automation core.** `automation.py`, pure over the Gateway. Sequence: assert
docked-at-start (abort if already cleaning) -> set carpet RISE -> run vacuum routine ->
depart-then-return poll -> set carpet AVOID -> run mop routine. The poll:
  - Depart: wait until `state_name=="cleaning"` (start-timeout, e.g. 3 min) so we know it left.
  - Return: wait until `docked and not in_cleaning` (long timeout, e.g. 120 min). Charging
    alone is NOT done — recharge-resume keeps `in_cleaning` true.
  - Early-exit terminals: `error is not None` -> raise; unexpected `idle` without progress.
Retry helper (few attempts, backoff) wraps each device call. Verify: `test_automation.py`
green including (a) still-looks-docked right after trigger, (b) charging-then-resumes
mid-job does NOT advance to mop, (c) error state aborts, (d) not-docked-at-start aborts.

**P3 CLI scripts.** `login.py`, `discover.py`, `run.py` wired to the gateway factory.
Verify: `GATEWAY=fake uv run python -m roborock_auto.discover` prints fake device + rooms +
routines + carpet value; `run.py` completes the two-stage sequence against the fake clock.

**P4 Backend.** FastAPI app, one shared gateway created in a `lifespan` handler
(`connect()` on startup, `close()` on shutdown). Endpoints: `GET /status`, `/map.png`,
`/map-data`, `/rooms`, `/routines`, `/consumables`; `POST /start /pause /dock /clean/rooms
/fan /mop /carpet /routine`. Bind 127.0.0.1. `/map.png` only re-reads bytes when
`map-data` changed (cache last render). Verify: `test_api.py` green.

**P5 Frontend.** One `static/index.html`: vanilla HTML + JS, no build step. Status card,
map `<img src="/map.png">`, control buttons, room checkboxes from `/rooms`, routine buttons
from `/routines`, consumables. A `setInterval` re-fetches `/status` (and bumps the map img)
every `POLL_INTERVAL_SEC`. Verify: `uv run` the app, load in browser, click start and watch
the fake robot go docked -> cleaning -> docked over the next polls.

**P6 LiveGateway.** Implement against the confirmed API; `GATEWAY=live` selects it. All
device I/O serialized by a single `asyncio.Lock` (single-user, cheap). `connect()` builds
the DeviceManager + web client; the retry helper reconnects on a dropped session. Maps
`RoborockStateCode` + status flags -> `DeviceStatus`; maps semantic enums -> config ints.
Verify: `test_live_mapping.py` green — mocked `v1_properties`/`RoborockApiClient` assert the
exact command + params sent for each method and the state-code -> state_name table. (No live
integration test is possible without an account; this is the account-dependent piece.)

**P7 Cron workflow + README.** `clean.yml` (schedule, uv sync, run.py, secret-injected
session). README: how to log in, run discovery, fill `.env`, launch the app, switch to live.
Verify: workflow yaml parses; README steps match reality.

## Testing strategy (no account)

- FakeGateway is the test double AND the local-run backend — one thing, exercised two ways.
- Contract test asserts FakeGateway satisfies every Gateway method with sane types.
- The clock is injected, so the depart-then-return poll and its timeouts are deterministic
  with no real sleeps.
- API tests hit FastAPI TestClient. No WebSocket, so no ws-timing flakiness.
- LiveGateway: mocked-object unit tests assert exact commands/params and the state mapping.

## Risks

- python-roborock trait API is evolving; `LiveGateway` is the fragile part and is quarantined
  behind the Gateway so a library change can't ripple into the app or tests.
- Python 3.14 is too new for the dep wheels -> pin 3.12 via uv.
- Session token expiry has no headless re-auth; fail loudly so the user re-runs login.py.
- Recharge-resume is the subtle correctness case; `in_cleaning` in the completion predicate
  is what prevents the mop stage firing on a mid-job charge.
```
