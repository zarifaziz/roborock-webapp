# ADR 0001: Talk to the Roborock cloud API directly

**Status:** accepted

## Context

The goal is a two-stage clean that never wets carpet: vacuum with carpet mode set to
Rise (mop lifts, carpet still vacuumed), wait for that job to actually finish, then mop
with carpet mode set to Avoid (robot skips carpet entirely).

Carpet mode is the problem. It is a persistent, map-level setting on the device
(Map Management, per-carpet-area), not a per-job parameter. Nothing that drives the robot
from outside can change it unless it can reach a Roborock-proprietary property.

Every off-the-shelf route was checked and ruled out:

- **Roborock Custom Routines.** A routine bundles rooms, clean mode, and a schedule. It has
  no field for carpet avoidance, so a routine cannot flip the setting as part of running.
- **Matter / Apple HomeKit.** The Matter Robot Vacuum Cleaner device type exposes only
  start, pause, resume, dock, and room select. Carpet mode is not in that device type, so
  native Matter, Homebridge, ATH Bridge, and Siri Shortcuts all cannot touch it.
- **Home Assistant.** Its Roborock integration *can* do this, because it bypasses Matter and
  speaks to Roborock's cloud API through `python-roborock`. The HA install was
  decommissioned and is not coming back for one automation.

## Decision

Use `python-roborock` directly, the same library Home Assistant's integration is built on.

Two consequences follow from it. The library reaches Roborock's **cloud** API rather than
the home LAN, so this can run anywhere, including a scheduled GitHub Actions workflow with
no always-on machine at home. And because it is an unofficial, undocumented API, the vendor
edge is quarantined behind an anti-corruption layer (see ARCHITECTURE.md).

## Consequences

- No home server needed. Local cron on a laptop was explicitly rejected: a machine that is
  asleep, closed, or off-network means the automation silently does not fire.
- The API is undocumented and can break on a Roborock-side change. Calls carry retries and
  fail loudly rather than degrading quietly.
- Auth is an interactive email one-time code, so there is no headless re-auth. The session
  is persisted and reused; when it expires a human must re-run the login flow.
- Firmware-specific values (the integers behind Rise and Avoid, the device `duid`, scene
  ids) are discovered against the real device and held in config, never guessed in code.
