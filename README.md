# roborock-webapp

Local control app and unattended carpet-aware two-stage clean for a Roborock Qrevo L Pro.

The whole thing runs against a `FakeGateway` with no account, so you can try it on your
laptop right now. Going live is filling in `.env` and setting `GATEWAY=live`; no code change.

- Landing page: https://site-mu-ten-70.vercel.app (source in `site/`)
- Architecture (ports & adapters + DDD-lite): `ARCHITECTURE.md`

## Try it now (fake mode, no account)

```
uv sync --extra dev
uv run uvicorn roborock_auto.adapters.web.server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

You get a "Connect your Roborock" panel, a status card, a live map with the robot position,
start/pause/dock, room selection, fan/mop/carpet controls, saved routines, and the headline
one-click **"Vacuum, then mop avoiding carpet"** two-stage clean with live stage progress. The
fake robot advances on a clock (docked -> cleaning -> docked) so the UI actually moves; the
two-stage run takes about a minute in fake mode.

The app also schedules itself in-process: set Room Intervals to see an Overdue view, or
create a weekly Schedule Rule, and a background scheduler tick fires due Occurrences with
no cron needed. See "Scheduling" below.

Run the tests:

```
uv run pytest
```

## Go live (needs your Roborock account)

1. Set `GATEWAY=live` and `ROBOROCK_EMAIL` in `.env` (copy from `.env.example`).
2. Log in once. This sends an email code and saves the session to `user_data.json`
   (gitignored, never commit it). Either use the "Connect your Roborock" panel in the app
   (Send code -> enter the emailed code -> Connect), or the CLI:
   ```
   uv run python -m roborock_auto.adapters.cli.login
   ```
3. Discover your ids. This is read-only, it changes nothing on the robot:
   ```
   uv run python -m roborock_auto.adapters.cli.discover
   ```
   From the output, copy into `.env`: your `ROBOROCK_DUID` and the carpet integer for
   `CARPET_RISE_VALUE` / `CARPET_AVOID_VALUE` (toggle carpet mode in the Roborock app and
   re-run discover to see both values). Live mode refuses to start until these are set.
   The app's "Discover" button shows the same read-only snapshot in the browser, but capturing
   the `ROBOROCK_DUID` and carpet integers into `.env` stays this manual CLI step for now.
4. Launch the app the same way as fake mode. It now talks to the real robot.

The `RoborockCloudAdapter` command names are verified against python-roborock, but a few
param shapes (segment clean, fan, mop) are marked "confirm on live bring-up" in
`adapters/roborock_cloud/` and should be checked on your first real run.

## Scheduling

Recurring cleans no longer need the GitHub Actions cron below; the app can schedule itself.

- **Room Intervals**: `POST /intervals` says how often a segment should be cleaned. The
  app derives last-cleaned from every Clean it has ever started (any entry point) and
  scores each interval segment's Priority; `GET /overdue` lists them worst-first, and
  `POST /clean/overdue` cleans everything currently Overdue in one call.
- **Schedule Rules**: `POST /schedules` names a weekly grid of days plus a time of day, and
  an Action (clean the whole house or chosen segments, in a chosen mode). A background
  scheduler task, running in the same FastAPI process, Materialises each Rule into
  Occurrences and fires them through the identical code path the UI button uses. No
  external trigger, no workflow file to edit.
- `GET /scheduler/status` reports the last tick, the resolved timezone, and whether the
  scheduler is erroring; `GET /occurrences` is the firing timeline.

Requires the `TIMEZONE` setting (an IANA zone name, e.g. `Australia/Sydney`) in every mode,
including fake — see `.env.example`. There is no default: an implicit "system local"
resolves to UTC inside a container and silently shifts every schedule.

## Unattended two-stage clean

`uv run python -m roborock_auto.adapters.cron.run` runs the sequence once: carpet Rise, vacuum routine,
wait for the robot to leave and return (a mid-job recharge does not count as done), then
carpet Avoid, mop routine. It exits non-zero on failure.

`.github/workflows/clean.yml` runs this on a schedule. It is now one option among several
for triggering a Two-Stage Clean, not the only way to schedule anything — a Schedule Rule
above covers most recurring cleans without a workflow file. It is disabled by default;
enable the cron and add the `ROBOROCK_USER_DATA` secret plus the repo variables if you
still want it.
