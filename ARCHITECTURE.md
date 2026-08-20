# Architecture: ports & adapters with a DDD-lite core

Target device: **Roborock Qrevo L Pro** (single device, single user, cloud MQTT via
python-roborock). This document defines the abstractions we build everything else on, and
the rule for deciding where new code goes.

## 1. Honest scoping of DDD

For robot state — status, settings, maps — the robot's firmware stays the source of truth;
our app reads it, decides, and commands it, so most tactical DDD machinery around *that*
state is still ruled out:

- **No repositories / aggregates with identity for robot state** — there is nothing to
  persist and rehydrate. The one "aggregate" for that slice is the robot itself, and its
  state lives in firmware.
- **No domain events / event sourcing** — one user, one device, no downstream consumers.
- **What we DO take from DDD:** ubiquitous language, value objects, domain policies
  (pure functions encoding rules), one long-running domain process (the two-stage clean),
  bounded contexts as *module boundaries*, and an anti-corruption layer at the vendor edge.

SCHEDULING is the exception this section used to predict: Room Intervals, Schedule Rules,
Occurrences, and the Cleaning Run log are real owned state, the firmware has no memory of
any of it, so that context does earn repository-shaped ports (`OccurrencePort`,
`ScheduleRulePort`, `RoomIntervalPort`, `CleaningLogPort`) backed by SQLite (§4, §7).

## 2. The dependency rule

```
            ┌────────────────────────────────────────────────────┐
            │                    ADAPTERS                        │
            │  driving (call in)          driven (called out to) │
            │  ─────────────────          ────────────────────── │
            │  webapp (FastAPI)           roborock_cloud (live)  │
            │  cron runner (run.py)       fake (tests, demo UI)  │
            │  cli (login, discover)                             │
            └───────────┬───────────────────────▲────────────────┘
                        │ calls                 │ implements
            ┌───────────▼───────────────────────┴────────────────┐
            │                 APPLICATION                        │
            │  use cases: StartClean, CleanRooms, TwoStageClean, │
            │  ChangeSettings, GetPanelSnapshot, ...             │
            │  owns: retry policy, port orchestration            │
            └───────────┬───────────────────────▲────────────────┘
                        │ uses                  │ defined here
            ┌───────────▼────────┐   ┌──────────┴────────────────┐
            │      DOMAIN        │   │         PORTS             │
            │  value objects,    │   │  Protocols the domain/app │
            │  policies, the     │   │  need the world to honor  │
            │  cleaning process  │   │  (split by role, §4)      │
            └────────────────────┘   └───────────────────────────┘

  Arrows of source-code dependency point INWARD only.
  domain imports nothing from app/adapters. application imports domain + ports.
  adapters import everything. Nothing imports adapters except main/wiring.
```

The single load-bearing rule: **`roborock.*` (the vendor library) is imported in exactly
one package: `adapters/roborock_cloud/`.** Everything else speaks our own types. This is
the anti-corruption layer, and it already exists today (`models.py` + `LiveGateway`); we
are keeping it and formalizing it.

The same rule shape confines persistence: **`sqlite3` is imported in exactly one package:
`adapters/sqlite/`.** The four SCHEDULING/CONTROL store ports (`OccurrencePort`,
`ScheduleRulePort`, `RoomIntervalPort`, `CleaningLogPort`) speak our own domain types;
nothing above this adapter knows it is SQLite.

## 3. Bounded contexts (as modules, not services)

Four contexts, each a vertical slice through domain/application/ports. They share the
value-object vocabulary but grow independently:

```
 CONTROL            MAPPING              MAINTENANCE          SCHEDULING
 ────────           ────────             ────────────         ──────────
 status, start/     map snapshot,        consumables,         Room Intervals,
 pause/dock,        rooms, zones,        dock actions         Overdue view,
 room/zone clean,   robot position,      (dust collect,       Schedule Rules,
 fan/mop/carpet,    (later: no-go        mop wash, dry)       Occurrences, the
 routines,          zones, virtual                            in-process scheduler
 two-stage clean    walls, split/merge)
```

The Cleaning Run log stays in CONTROL, not SCHEDULING, even though the Overdue view
reads it: a Clean is a CONTROL concept that exists whether or not scheduling does, and
folding the log into a scheduling port would make the whole-house button depend on the
scheduler's storage, crossing the boundary the other way.

They stay in one process and one repo. "Context" here means: a new feature must name its
context, and code in one context never reaches into another's internals — it goes through
that context's application layer.

## 4. Ports: split by role, one adapter implements all

Today's `Gateway` protocol (13 methods) is one interface. With dock actions, map editing,
DND, volume, and child lock coming, it becomes a god interface where every consumer sees
every method and every fake must stub everything. We split it by consumer role (interface
segregation), keeping **semantic values in, plain dataclasses out**:

```python
# ports/robot.py
class RobotStatusPort(Protocol):
    async def get_status(self) -> DeviceStatus: ...

class CleaningControlPort(Protocol):
    async def start(self) -> None: ...
    async def pause(self) -> None: ...
    async def dock(self) -> None: ...
    async def clean_rooms(self, segment_ids: list[int]) -> None: ...
    async def clean_zones(self, zones: list[Zone]) -> None: ...        # planned

class CleaningSettingsPort(Protocol):
    async def set_fan_power(self, power: FanPower) -> None: ...
    async def set_mop_mode(self, mode: MopMode) -> None: ...
    async def set_carpet_mode(self, mode: CarpetMode) -> None: ...
    async def get_carpet_mode(self) -> CarpetMode: ...

class RoutinePort(Protocol):
    async def list_routines(self) -> list[Routine]: ...
    async def run_routine(self, routine_id: int) -> None: ...

# ports/mapping.py
class MapPort(Protocol):
    async def get_map_png(self) -> bytes: ...
    async def get_map_data(self) -> MapData: ...
    async def list_rooms(self) -> list[Room]: ...

# ports/maintenance.py
class MaintenancePort(Protocol):
    async def get_consumables(self) -> Consumables: ...
    async def start_dust_collection(self) -> None: ...                 # planned
    async def start_mop_wash(self) -> None: ...                        # planned
    async def start_drying(self) -> None: ...                          # planned

# lifecycle stays its own concern
class ConnectionPort(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
```

**One concrete adapter still implements all of them.** `RoborockCloudAdapter` (today's
`LiveGateway`) satisfies every protocol structurally — Python needs no inheritance
gymnastics for this. The win is on the consumer side:

- `TwoStageClean` depends on `RobotStatusPort + CleaningSettingsPort + RoutinePort` and
  nothing else. Its test fake implements 3 small protocols, not 20 methods.
- The webapp's map endpoints depend only on `MapPort`.
- SCHEDULING proved the pattern: `ScheduleRulePort`, `OccurrencePort`, and
  `RoomIntervalPort` (§2) were added later and touch zero of the ports above.

Rule for growing a port: a new method goes in the port whose *consumer role* needs it,
not wherever is convenient. If no existing port fits, add a new small one.

## 5. Domain layer: what is genuinely pure here

`domain/` holds three kinds of things, all import-free of I/O:

**Value objects** (today's `models.py`, moved): `DeviceStatus`, `Room`, `Zone`, `Point`,
`MapData`, `Consumables`, `Routine`, and the semantic enums `FanPower`, `MopMode`,
`CarpetMode`. Frozen dataclasses, no vendor types, no magic integers.

**Policies** — named pure functions encoding the rules we learned about this robot.
These are the ubiquitous language; the mid-job-recharge insight currently buried in
`live.py:derive_in_cleaning` is a *domain rule*, not an adapter detail:

```python
# domain/policies.py
def job_is_active(status: DeviceStatus) -> bool:
    """A mid-job recharge keeps the job active: docked+charging does not mean done."""
    return status.in_cleaning

def home_and_idle(status: DeviceStatus) -> bool:
    return status.docked and not status.in_cleaning

def ready_to_start(status: DeviceStatus) -> bool:
    return status.docked and not status.in_cleaning and status.error is None
```

The adapter's job shrinks to honest translation: firmware state codes -> the semantic
fields of `DeviceStatus`. The *interpretation* of those fields lives in policies. When a
policy needs richer input later (e.g. distinguishing `returning_home`), we extend
`DeviceStatus`, never leak state codes.

**The cleaning process** — the two-stage clean is our one real domain process. It stays
what it is today (`automation.py`): a deterministic procedure over ports with injected
`clock`/`sleep`, tested without real waits. It moves to
`application/two_stage_clean.py` and its wait predicates become calls to the policies
above. No state-machine framework; the linear procedure with depart/return phases *is*
the correct expression of this process at its current complexity. If we later add
pause/resume/abort mid-process, we revisit.

**Capabilities** — one value object pins us to the Qrevo L Pro without pretending to be
model-generic:

```python
# domain/capabilities.py
@dataclass(frozen=True)
class DeviceCapabilities:
    has_camera: bool
    has_mop_wash_dock: bool
    has_drying: bool
    carpet_modes: frozenset[CarpetMode]

QREVO_L_PRO = DeviceCapabilities(
    has_camera=False,
    has_mop_wash_dock=True,
    has_drying=True,
    carpet_modes=frozenset({CarpetMode.RISE, CarpetMode.AVOID}),
)
```

The webapp asks capabilities what to render; use cases guard on them. When a second
model ever appears, the adapter selects the profile — no code path speculates about it
before then.

## 6. Application layer: use cases own orchestration and retries

Thin async functions that take ports as parameters, apply policies, and sequence
commands. HTTP knows nothing about retries; adapters know nothing about sequencing.

```
application/
  two_stage_clean.py  # the PLAN.md automation; waits expressed over domain policies
  retry.py            # workflow-level retry helper + AutomationError
```

A use case exists where there is real orchestration. Trivial one-command endpoints
(start, pause, fan power) go straight from the driving adapter to the port — a use-case
function that only forwards a call is ceremony, and we skip it until a guard or sequence
gives it a job. `two_stage_clean` is the template for when one does.

The retry split: **transport-level** retry stays in the adapter, which owns the
connection (and deliberately does not reconnect per failure — the cloud login endpoint is
rate limited); **workflow-level** retry (should this step be reattempted at all?) lives
in `retry.py`. One helper each, no duplication.

## 7. Adapters

```
adapters/
  roborock_cloud/       # DRIVEN. The only package importing roborock.*
    adapter.py          # RoborockCloudAdapter: implements all ports; lock + reconnect
    codes.py            # firmware integer/state-code tables (fan levels, carpet values)
    session.py          # user_data.json load/save (today's core.py)
  fake/                 # DRIVEN. In-memory robot honoring the same ports; drives tests
    fake_robot.py       #   and `FAKE=1` demo mode for frontend work with no account
  sqlite/                # DRIVEN. The only package importing sqlite3
    database.py          # open_database(), append-only MIGRATIONS list
    cleaning_log.py      # SqliteCleaningLog: CleaningLogPort
    occurrences.py       # SqliteOccurrences: OccurrencePort
    room_intervals.py    # SqliteRoomIntervals: RoomIntervalPort
    schedule_rules.py    # SqliteScheduleRules: ScheduleRulePort
  web/                  # DRIVING. FastAPI app; translates HTTP <-> use cases; serves SPA
  cron/                 # DRIVING. run.py entrypoint for the GitHub Actions clean
  cli/                  # DRIVING. login.py, discover.py
```

Wiring happens in one place per entrypoint (FastAPI lifespan, cron main): build the
adapter, hand the same object to every use case as each of its port types. No DI
framework — constructor injection and Python's structural typing are enough at this size.

The **fake adapter is a first-class citizen**: it simulates depart/clean/return
transitions on a fake clock, which is what lets the two-stage tests and the frontend demo
run in milliseconds. Every new port method lands in the fake in the same PR that adds it
to the live adapter — that discipline is what keeps the ports honest.

## 8. Layout (as built)

```
src/roborock_auto/
  config.py             # env-driven settings, shared by wiring and adapters; TIMEZONE required
  domain/
    models.py           # value objects + enums, incl. Occurrence, ScheduleRule, RoomInterval
    policies.py         # job_is_active, home_and_idle, ready_to_start
    capabilities.py     # DeviceCapabilities + QREVO_L_PRO
  ports/
    __init__.py         # re-exports + RobotGateway (union of the robot ports, for wiring)
    robot.py            # RobotStatusPort, CleaningControlPort, CleaningSettingsPort, RoutinePort
    mapping.py          # MapPort
    maintenance.py      # MaintenancePort
    lifecycle.py        # ConnectionPort
    cleaning_log.py     # CleaningLogPort (CONTROL: records every Clean, any entry point)
    occurrences.py      # OccurrencePort (SCHEDULING: the scheduler's store)
    room_intervals.py   # RoomIntervalPort (SCHEDULING: the Overdue view's store)
    schedule_rules.py   # ScheduleRulePort (SCHEDULING: the schedules CRUD's store)
  application/
    two_stage_clean.py  retry.py
    start_clean.py       # the shared clean use case, every entry point routes through it
    materialise.py       # Schedule Rule -> forward Occurrence rows over the 14-day horizon
    overdue.py           # Priority scoring + the Overdue report
    scheduler.py         # the tick: materialise, select, gate, claim, fire, record
  adapters/
    __init__.py         # get_gateway(config): picks live or fake
    sample_map.py       # placeholder PNG shared by fake and the live map fallback
    roborock_cloud/     # adapter.py  codes.py  session.py
    fake/               # fake_robot.py (FakeRobot)
    sqlite/             # database.py  cleaning_log.py  occurrences.py  room_intervals.py  schedule_rules.py
    web/                # server.py  static/
    cron/               # run.py
    cli/                # login.py  discover.py
```

Entrypoints: `uvicorn roborock_auto.adapters.web.server:app`,
`python -m roborock_auto.adapters.cron.run`, `python -m roborock_auto.adapters.cli.login`,
`python -m roborock_auto.adapters.cli.discover`.

## 9. Test layout

- `test_policies.py` — domain rules stated as documentation
- `test_two_stage_clean.py` — the process over `FakeRobot` with an injected clock
- `test_fake_robot.py` — FakeRobot honors the full RobotGateway contract
- `test_cloud_adapter_mapping.py` — pins each command/param the live adapter sends
- `test_api.py` — HTTP endpoints over the fake, via TestClient
- `test_scheduler.py` — the scheduler tick over `FakeRobot` and a real in-memory database,
  with an injected wall clock
- `test_scheduling_store.py` — the store-port contract, parameterised over adapters
  (ADR 0006: no fake store, since a dict cannot honour the claim's atomicity)

## 10. Rules of thumb going forward

- New feature? Name its **context** (§3), pick or add the **port** its consumer role
  needs (§4), express robot-behavior knowledge as a **policy** (§5), sequence it in a
  **use case** (§6), implement it in **both adapters** (§7) in the same change.
- A vendor type or firmware integer outside `adapters/roborock_cloud/` is a bug.
- A `sqlite3` import outside `adapters/sqlite/` is the same bug, one layer over.
- Map-*write* features (no-go zones, split/merge) get their own port
  (`MapEditPort`) and a confirmation step in the driving adapter — they are the one
  category that can destroy user data.
- Resist abstraction on spec: one device, one user, one process. Every layer above
  exists because it already pays for itself in tests or planned features, not because
  the pattern book has a slot for it.
