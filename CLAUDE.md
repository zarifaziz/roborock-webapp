# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```
uv sync --extra dev                                    # install
uv run pytest                                          # all tests
uv run pytest tests/test_policies.py::test_name        # one test
uv run ruff check . && uv run ruff format .            # lint + format
uv run uvicorn roborock_auto.adapters.web.server:app --host 127.0.0.1 --port 8000
```

The app defaults to `GATEWAY=fake`, so it runs and all tests pass with no Roborock account
and no `.env`. Live mode (`GATEWAY=live`) also needs `ROBOROCK_EMAIL`, `ROBOROCK_DUID`,
`CARPET_RISE_VALUE`, `CARPET_AVOID_VALUE`, plus a `user_data.json` session written by
`uv run python -m roborock_auto.adapters.cli.login`. `python -m roborock_auto.adapters.cli.discover`
prints the ids and carpet integers read-only.

Other entrypoints: `python -m roborock_auto.adapters.cron.run` (the unattended two-stage
clean, also run by `.github/workflows/clean.yml`).

`pytest` runs in `asyncio_mode = "auto"`, so async tests need no marker.

## Working tree

Do all feature and fix work in a git worktree under `.worktrees/`, never in the repo root.
The repo root stays on `main`.

## Architecture

Ports and adapters with a DDD-lite core, one device (Roborock Qrevo L Pro), one user.
`ARCHITECTURE.md` is the full document and the source of truth for where new code goes.
The parts that decide most changes:

**The vendor library `roborock.*` is imported in exactly one package,
`adapters/roborock_cloud/`.** A vendor type or firmware integer anywhere else is a bug.
Everything outside that package speaks the frozen dataclasses and enums in `domain/models.py`.

**Dependencies point inward.** `domain/` imports nothing from app or adapters;
`application/` imports domain and ports; adapters import everything; only wiring imports
adapters (`adapters/__init__.py:get_gateway` picks live vs fake from config).

**Ports are split by consumer role,** not by adapter (`ports/robot.py`, `mapping.py`,
`maintenance.py`, `lifecycle.py`, `auth.py`). One concrete adapter satisfies all of them
structurally. A new method goes in the port whose consumer needs it; add a small new port
if none fits.

**Knowledge about how this robot behaves goes in `domain/policies.py`, not in the adapter.**
The adapter only translates firmware codes to the semantic fields of `DeviceStatus`
(`adapters/roborock_cloud/codes.py`); the interpretation lives in the policies. The
load-bearing example: a mid-job recharge leaves the job active, so docked plus charging
does not mean done.

**The fake adapter is first class.** `adapters/fake/fake_robot.py` simulates
depart/clean/return on a fake clock, which is what makes `test_two_stage_clean.py` and the
demo UI run in milliseconds. Every new port method lands in the fake in the same change
that adds it to the live adapter.

**Use cases exist only where there is real orchestration.** `application/two_stage_clean.py`
is the one long-running process; trivial one-command endpoints go straight from the web
adapter to the port. Transport retry lives in the adapter (it deliberately does not
reconnect per failure, the cloud login endpoint is rate limited); workflow retry lives in
`application/retry.py`.

Map-write features (no-go zones, split/merge) get their own port and a confirmation step in
the driving adapter, since they can destroy user data.

## Tests

`test_policies.py` states the domain rules, `test_cloud_adapter_mapping.py` pins the exact
command names and params the live adapter sends, `test_api.py` drives the HTTP endpoints
over the fake, `test_fake_robot.py` checks the fake honors the full gateway contract.
Prefer asserting on complete outputs over fragment checks.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `zarifaziz/roborock-webapp`, read and written
via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage labels, used unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
