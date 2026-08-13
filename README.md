# roborock-auto

Local control app and unattended carpet-aware two-stage clean for a Roborock Qrevo L Pro.

The whole thing runs against a `FakeGateway` with no account, so you can try it on your
laptop right now. Going live is filling in `.env` and setting `GATEWAY=live`; no code change.

- Architecture (ports & adapters + DDD-lite): `ARCHITECTURE.md`
- Design: `MVP_PLAN.md`
- Background and why this exists: `handoff.md`

## Try it now (fake mode, no account)

```
uv sync --extra dev
uv run uvicorn roborock_auto.adapters.web.server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

You get a status card, a live map with the robot position, start/pause/dock, room selection,
fan/mop/carpet controls, and buttons to trigger saved routines. The fake robot advances on a
clock (docked -> cleaning -> docked) so the UI actually moves.

Run the tests:

```
uv run pytest
```

## Go live (needs your Roborock account)

1. Set `GATEWAY=live` and `ROBOROCK_EMAIL` in `.env` (copy from `.env.example`).
2. Log in once. This sends an email code and saves the session to `user_data.json`
   (gitignored, never commit it):
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
4. Launch the app the same way as fake mode. It now talks to the real robot.

The `RoborockCloudAdapter` command names are verified against python-roborock, but a few
param shapes (segment clean, fan, mop) are marked "confirm on live bring-up" in
`adapters/roborock_cloud/` and should be checked on your first real run.

## Unattended two-stage clean

`uv run python -m roborock_auto.adapters.cron.run` runs the sequence once: carpet Rise, vacuum routine,
wait for the robot to leave and return (a mid-job recharge does not count as done), then
carpet Avoid, mop routine. It exits non-zero on failure.

`.github/workflows/clean.yml` runs this on a schedule. It is disabled by default; enable the
cron and add the `ROBOROCK_USER_DATA` secret plus the repo variables once you are ready.
