# Implementation Plan: Roborock carpet-aware two-stage clean

This is the build plan derived from `handoff.md`. The goal, the "why", and the ruled-out
alternatives live there and are not repeated. This document is the concrete how: real API
calls, file layout, phase order, and the failure cases we design for.

## API facts confirmed against the library

These were verified against the current `python-roborock` source (the trait-based
`DeviceManager` API), not guessed. Two things are genuinely undiscoverable until we run
against the real device, and phase 1 exists to discover them.

Login and session (module `roborock.web_api`):

- `RoborockApiClient(username=email)` then `await client.request_code_v4()` sends the email
  code. `await client.code_login_v4(code)` returns a `UserData` object.
- `UserData` serializes with `user_data.as_dict()` and reloads with
  `UserData.from_dict(d)`. That is our persistence: dump to JSON once, reload every run.
  No re-login unless the token is rejected.

Devices (module `roborock.devices.device_manager`):

- `UserParams(username=email, user_data=user_data)` then
  `device_manager = await create_device_manager(user_params)`.
- `await device_manager.get_devices()` returns `RoborockDevice` objects. Each has `.name`
  and `.duid`. The Qrevo L Pro is a V1 device, so `device.v1_properties` is set.
- Always `await device_manager.close()` in a finally block.

Commands and status (on `device.v1_properties`):

- Status: `status_trait = device.v1_properties.status`, `await status_trait.refresh()`,
  then read `status_trait.state` (a `RoborockStateCode`).
- Arbitrary command: `await device.v1_properties.command.send(command, params=...)`.
  `command` accepts a `RoborockCommand` enum member or a string.
- Carpet mode uses `RoborockCommand.GET_CARPET_CLEAN_MODE` and
  `RoborockCommand.SET_CARPET_CLEAN_MODE`. The param shape and the integer that means
  "Rise" vs "Avoid" are NOT defined as named enums in the library. We read the current
  value first, change it in the app once to see how the value moves, and lock the mapping
  in phase 1.

Scenes / Custom Routines (on `RoborockApiClient`, the cloud web API — separate from the
device connection):

- `await client.get_scenes(user_data, device_id)` returns `HomeDataScene` objects (each has
  an id and a name). `device_id` here is the device's home-data id, which is the `duid`.
- `await client.execute_scene(user_data, scene_id)` triggers a routine.

State codes for the completion poll:

- `RoborockStateCode` is an int enum. The docked/charging state is `charging` (community
  value 8) and idle is a low value. We do not hardcode a guessed integer. Phase 1 prints
  the live `status_trait.state` while docked and while cleaning so we pin the exact members
  by name (`RoborockStateCode.charging`, `.cleaning`, etc.) rather than magic numbers.

## What has to be discovered at runtime (the whole reason phase 1 comes first)

1. The `duid` of the Qrevo L Pro.
2. The two `scene_id`s: one for the vacuum-only routine, one for the mop-only routine.
3. The carpet-mode param format and which value means Rise and which means Avoid.
4. The exact `RoborockStateCode` members for docked and for actively cleaning.

Guessing any of these and then writing to the device is the one thing we refuse to do.

## Repo layout

```
roborock-automation/
  pyproject.toml            # uv-managed; dep: python-roborock
  README.md                 # short run instructions
  .gitignore                # ignores user_data.json and .env
  src/roborock_auto/
    __init__.py
    session.py              # load/save UserData, build DeviceManager + web client
    login.py                # phase 1a: interactive one-time login -> user_data.json
    discover.py             # phase 1b: read-only dump of duid, scenes, carpet mode, state
    run.py                  # phase 3: the scheduled two-stage clean
    config.py               # reads env: email, scene ids, carpet values, state codes
  .github/workflows/
    clean.yml               # phase 4: scheduled cron trigger
```

Tooling: `uv` with `pyproject.toml`. `uv run python -m roborock_auto.discover` locally;
the workflow uses `uv sync` then `uv run`.

Session persistence: `user_data.json` written locally by the login script, never committed
(gitignored). For the scheduled run it is stored as a GitHub Actions secret and written to
disk at job start.

## Phase 1 — login + discovery (build this first, per the handoff)

Success criteria: run two scripts locally and see, printed to the terminal, the duid, both
scene ids with their names, the current carpet mode value, and the live state code while
docked. No write to the device happens in this phase.

1a. `login.py` — interactive, run once by the user:
- `RoborockApiClient(email)`, `request_code_v4()`, prompt for the code, `code_login_v4()`.
- Write `user_data.as_dict()` to `user_data.json`. Print "saved, do not commit this file".
- Verify: file exists and re-parses via `UserData.from_dict`.

1b. `discover.py` — read-only:
- Load `user_data.json`, build `DeviceManager`, `get_devices()`, print each name + duid.
- For the Qrevo (v1_properties present): `command.send(RoborockCommand.GET_CARPET_CLEAN_MODE)`
  and print the raw response so we learn the value shape.
- `status.refresh()` and print `status_trait.state` and its `.name` (device is docked now).
- Build `RoborockApiClient`, call `get_scenes(user_data, duid)`, print each scene's id + name.
- Verify: user reads the output and tells us which scene id is vacuum-only and which is
  mop-only, and we note the docked state code.

Manual step between 1b and 2: user toggles carpet mode in the Roborock app (Rise, then
Avoid) and reruns the `GET_CARPET_CLEAN_MODE` line each time so we capture both integer
values. This is faster and safer than blind writes.

## Phase 2 — confirm writes in isolation

Before wiring the full sequence, one tiny script (or a guarded flag in discover) that only
does `SET_CARPET_CLEAN_MODE` to the Rise value, then re-reads it, confirms the change, then
sets it back. This proves write access and the param format in isolation, with no cleaning
triggered. Success: read-back equals what we wrote.

## Phase 3 — the automation (`run.py`)

Config comes from env / config.py (no magic numbers in code): `DUID`, `VACUUM_SCENE_ID`,
`MOP_SCENE_ID`, `CARPET_RISE`, `CARPET_AVOID`, and the docked + cleaning state code names.

Sequence:

1. Load session, build device manager + web client.
2. Vacuum job: `SET_CARPET_CLEAN_MODE` -> Rise, read back to confirm, then
   `execute_scene(vacuum_scene_id)`.
3. Wait for completion with the **depart-then-return poll** (your choice):
   - Poll `status.refresh()` until state becomes a cleaning/active state (the robot has
     actually left the dock). Guard with a timeout (e.g. 3 min) in case the scene never
     started.
   - Then poll until state returns to the docked/charging state. Guard with a long timeout
     (e.g. 90 min) so a stuck robot fails loudly instead of hanging the job forever.
   - This avoids the trap where the robot still reads "docked" in the seconds right after
     the trigger and we falsely think the job already finished.
4. Mop job: `SET_CARPET_CLEAN_MODE` -> Avoid, read back to confirm, then
   `execute_scene(mop_scene_id)`.
5. Optionally poll the mop job to completion too (same helper), then `close()`.

Cross-cutting:
- Every device/cloud call wrapped with a small retry (a few attempts, backoff), since this
  is an undocumented cloud API that can hiccup.
- Structured logging at each step (what state we saw, what we set). On any failure, exit
  non-zero so the scheduler surfaces it.
- Notification on failure handled by the scheduler layer (phase 4), not baked into the script.

## Phase 4 — schedule on GitHub Actions

- `clean.yml` on a `schedule:` cron. Job: checkout, `uv sync`, write `user_data.json` from
  the `ROBOROCK_USER_DATA` secret, `uv run python -m roborock_auto.run`.
- Config values from phases 1–2 (scene ids, carpet values, state codes) stored as repo
  secrets / workflow env, not committed.
- A failing job emails via GitHub's built-in workflow-failure notification; that is the
  failure alert, so no custom notifier is needed.
- Note in README: the session token can expire; if the job starts failing auth, rerun
  `login.py` locally and update the secret.

## Open risks to watch

- Token lifetime unknown. If `user_data` expires we need a re-login; there is no headless
  re-auth (email code is interactive). Mitigation: fail loudly on auth error so we know to
  refresh the secret.
- `set_carpet_clean_mode` param shape is per-firmware; phase 2 read-back is the guard.
- `execute_scene` triggers the whole saved routine (rooms + mode baked into the routine),
  so the routines themselves must already be set up correctly in the app. We rely on that,
  we do not build the routines via API.

## Build order recap

1. Scaffold repo (pyproject, uv, layout).
2. `login.py` -> `user_data.json`. (verify: reloads)
3. `discover.py` read-only dump. (verify: duid + scene ids + carpet value + state visible)
4. Manual carpet toggle to capture Rise/Avoid values.
5. Phase 2 write-and-readback proof.
6. `run.py` full sequence with depart-then-return poll and retries.
7. `clean.yml` scheduled workflow + secrets.
