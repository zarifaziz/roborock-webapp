# Architecture: ports & adapters with a DDD-lite core

Target device: **Roborock Qrevo L Pro** (single device, single user, cloud MQTT via
python-roborock). This document defines the abstractions we build everything else on, and
the rule for deciding where new code goes.

## 1. Honest scoping of DDD

This system has almost no persisted state of its own. The robot's firmware is the source
of truth for status, settings, maps, and schedules; our app reads it, decides, and
commands it. That rules out most tactical DDD machinery:

- **No repositories / aggregates with identity** — there is nothing to persist and rehydrate.
  The one "aggregate" is the robot itself, and its state lives in firmware.
- **No domain events / event sourcing** — one user, one device, no downstream consumers.
- **What we DO take from DDD:** ubiquitous language, value objects, domain policies
  (pure functions encoding rules), one long-running domain process (the two-stage clean),
  bounded contexts as *module boundaries*, and an anti-corruption layer at the vendor edge.

If a future feature creates real owned state (e.g. our own schedule store, cleaning
history), it earns a repository port then — not before.

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

## 3. Bounded contexts (as modules, not services)

Four contexts, each a vertical slice through domain/application/ports. They share the
value-object vocabulary but grow independently:

```
 CONTROL            MAPPING              MAINTENANCE          SCHEDULING (later)
 ────────           ────────             ────────────         ──────────
 status, start/     map snapshot,        consumables,         timers, DND,
 pause/dock,        rooms, zones,        dock actions         our cron plan
 room/zone clean,   robot position,      (dust collect,
 fan/mop/carpet,    (later: no-go        mop wash, dry)
 routines,          zones, virtual
 two-stage clean    walls, split/merge)
```

They stay in one process and one repo. "Context" here means: a new feature must name its
context, and code in one context never reaches into another's internals — it goes through
that context's application layer.

## 4. Ports: split by role, one adapter implements all

Today's `Gateway` protocol (13 methods) is one interface. With dock actions, schedules,
map editing, DND, volume, and child lock coming, it becomes a god interface where every
consumer sees every method and every fake must stub everything. We split it by consumer
role (interface segregation), keeping **semantic values in, plain dataclasses out**:

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
- Adding `SchedulePort` later touches zero existing consumers.

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

Thin async functions (or small classes) that take ports as parameters, apply policies,
and sequence commands. HTTP knows nothing about retries; adapters know nothing about
sequencing. Examples:

```
application/
  panel.py            # GetPanelSnapshot: status + map sig + consumables for the UI poll
  cleaning.py         # StartClean, PauseClean, DockRobot, CleanRooms (guard: ready_to_start)
  settings.py         # ChangeFanPower, ChangeMopMode, ChangeCarpetMode
  two_stage_clean.py  # the PLAN.md automation, unchanged semantics
  retry.py            # the one shared retry helper (today duplicated in automation.py + live.py)
```

The retry policy consolidates here: today `with_retry` lives in `automation.py` and a
second copy (`_locked_retry`, with reconnect) hides in the adapter. Split responsibilities
cleanly: **transport-level** retry/reconnect stays in the adapter (it owns the
connection); **workflow-level** retry (should this step be reattempted at all?) lives in
the use case. One helper each, no duplication.

## 7. Adapters

```
adapters/
  roborock_cloud/       # DRIVEN. The only package importing roborock.*
    adapter.py          # RoborockCloudAdapter: implements all ports; lock + reconnect
    codes.py            # firmware integer/state-code tables (fan levels, carpet values)
    session.py          # user_data.json load/save (today's core.py)
  fake/                 # DRIVEN. In-memory robot honoring the same ports; drives tests
    fake_robot.py       #   and `FAKE=1` demo mode for frontend work with no account
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

## 8. Target layout

```
src/roborock_auto/
  domain/
    models.py           # value objects + enums (moved from models.py)
    policies.py         # job_is_active, home_and_idle, ready_to_start, ...
    capabilities.py     # DeviceCapabilities + QREVO_L_PRO
  ports/
    robot.py            # RobotStatusPort, CleaningControlPort, CleaningSettingsPort, RoutinePort
    mapping.py          # MapPort
    maintenance.py      # MaintenancePort
    lifecycle.py        # ConnectionPort
  application/
    panel.py  cleaning.py  settings.py  two_stage_clean.py  retry.py
  adapters/
    roborock_cloud/     # adapter.py  codes.py  session.py
    fake/               # fake_robot.py
    web/                # server.py  static/
    cron/               # run.py
    cli/                # login.py  discover.py
```

## 9. Migration from today's code (mechanical, test-covered)

Current code maps almost one-to-one; this is a re-homing, not a rewrite:

1. `models.py` -> `domain/models.py`; extract `policies.py` from the predicates inside
   `automation.py` and `live.py`. Verify: existing tests pass with import updates only.
2. Split `gateway/base.py` into the `ports/` protocols. `LiveGateway` ->
   `adapters/roborock_cloud/adapter.py` (also absorb `core.py` as `session.py`, and move
   the `_FAN_LEVELS`/`_MOP_MODES`/state-code tables to `codes.py`). Verify:
   `test_live_mapping.py` green.
3. `gateway/fake.py` -> `adapters/fake/`; narrow each test's fake to the ports it uses.
4. `automation.py` -> `application/two_stage_clean.py`, waits rewritten over policies;
   pull the guard/settings/webapp glue into the other use-case modules. Verify:
   `test_automation.py`, `test_api.py` green.
5. `webapp/` + `run.py` -> `adapters/web/`, `adapters/cron/`; endpoints call use cases
   instead of the gateway directly.

## 10. Rules of thumb going forward

- New feature? Name its **context** (§3), pick or add the **port** its consumer role
  needs (§4), express robot-behavior knowledge as a **policy** (§5), sequence it in a
  **use case** (§6), implement it in **both adapters** (§7) in the same change.
- A vendor type or firmware integer outside `adapters/roborock_cloud/` is a bug.
- Map-*write* features (no-go zones, split/merge) get their own port
  (`MapEditPort`) and a confirmation step in the driving adapter — they are the one
  category that can destroy user data.
- Resist abstraction on spec: one device, one user, one process. Every layer above
  exists because it already pays for itself in tests or planned features, not because
  the pattern book has a slot for it.
