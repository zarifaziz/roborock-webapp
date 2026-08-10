# Handover: Roborock Qrevo L Pro carpet-aware clean automation

## Goal

Build and deploy a script that automates a two-stage clean on a Roborock Qrevo L Pro without wetting carpets:

1. **Vacuum job**: set carpet mode to "Rise" (mop lifts, vacuum still cleans carpet), then trigger the full-house vacuum routine.
2. **Mop job**: set carpet mode to "Avoid" (robot skips carpet entirely), then trigger the mop-only routine.

The mop job should only start once the vacuum job has actually finished (poll device status for idle/docked, don't just use a fixed delay).

## Why this is needed (context, don't re-litigate)

- Roborock's app lets you build Custom Routines (rooms + clean mode + schedule), but a routine has no field for carpet avoidance mode. Carpet mode is a persistent, map-level setting (Map Management → per-carpet-area setting), not a per-routine parameter.
- Native Matter/Apple HomeKit support for robot vacuums only exposes basic start/pause/resume/dock/room-select. Carpet mode is a Roborock-proprietary property, not part of the Matter Robot Vacuum Cleaner device type, so HomeKit automations (native Matter, Homebridge, ATH Bridge, Siri Shortcuts) cannot touch it either.
- Home Assistant's Roborock integration _can_ do this because it talks to Roborock's cloud/local API directly via the `python-roborock` library, not through Matter. User has since decommissioned their HA setup and doesn't want to bring it back just for this.
- Conclusion: build a small standalone script using `python-roborock` directly, since it only needs to reach Roborock's cloud API (not the home LAN), so it can run anywhere — including a free scheduled GitHub Actions workflow, no home server required.

## Confirmed technical building blocks

- Library: [`python-roborock`](https://github.com/Python-roborock/python-roborock) (pip installable). Actively maintained, is what Home Assistant's integration is built on.
- Auth: email + one-time code login flow (`RoborockApiClient`), which returns a `user_data` object that should be persisted and reused so later runs are non-interactive.
- The raw device command `set_carpet_clean_mode` / `get_carpet_clean_mode` exists at the protocol level (confirmed via python-miio docs, python-roborock, and the ioBroker Roborock adapter). Exact enum values for Rise/Avoid/Ignore/Cross need to be confirmed against this specific device/firmware once we're inside the library.
- Triggering a specific Custom Routine (not just "clean whole home") is possible via a `sceneId`, reverse-engineered by the community for Siri Shortcuts integration (see the Medium post "Hacking the Roborock iOS App for Siri Shortcuts Integration" and blog.alexbeals.com/posts/roborock-routines-and-ios-shortcuts). Need to enumerate the user's actual scene IDs via the API rather than guessing.
- Device identifier needed: `duid` (device ID), obtainable via the device manager / device list call.

## Planned build sequence

1. **One-time interactive login script** (run locally by the user): authenticate via email code, print/save the resulting `user_data` session token somewhere safe (not committed to any repo).
2. **Discovery script** (read-only, run locally by the user):
   - List devices → get `duid` for the Qrevo L Pro.
   - List/inspect existing custom Routines → get `sceneId` for the vacuum-only routine and the mop-only routine.
   - Call `get_carpet_clean_mode` to confirm read access and see current value/enum before attempting any writes.
3. **Automation script** (the actual thing that runs on schedule):
   - Vacuum job: `set_carpet_clean_mode` → Rise, then trigger vacuum-only scene.
   - Poll device status until idle/docked.
   - Mop job: `set_carpet_clean_mode` → Avoid, then trigger mop-only scene.
   - Wrap calls in retries; log/notify on failure (this is an unofficial, undocumented cloud API and could break on a Roborock-side change).
4. **Secrets handling**: session token stored as a GitHub Actions repo secret (or equivalent), injected as an environment variable at runtime. Never hardcoded or committed.
5. **Scheduling**: GitHub Actions `schedule:` cron trigger(s) — no home server or always-on machine required, since this only talks to Roborock's cloud, not the local network. (Explicitly ruled out: running from the user's laptop via local cron, since a laptop that's asleep/closed/off-network means the automation silently doesn't fire.)

## Status

Nothing has been built yet — this is a plan only. Next concrete step agreed with the user: write the **read-only discovery script** first (steps 1–2 above) so the user can confirm everything is reachable (login works, device found, routines found, carpet mode readable) before writing anything that changes device settings.

## User context relevant to this task

- Comfortable with Python (Lead AI Engineer by trade), fine with a code-first approach rather than needing a GUI tool.
- Prefers not to reinstall/maintain Home Assistant.
- Device: Roborock Qrevo L Pro.
- No em dashes in any drafted content for this user (a standing formatting preference).
