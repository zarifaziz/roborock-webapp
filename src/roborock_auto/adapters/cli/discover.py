"""Read-only dump of everything the automation needs to be configured.

    uv run python -m roborock_auto.adapters.cli.discover

Works in both modes. In live mode it prints the real duid, routines, and current carpet
value so you can fill in .env. Nothing is written to the device. Each section is independent:
if one call fails on your firmware, the rest still print.
"""

from __future__ import annotations

import asyncio

from roborock_auto.adapters import get_gateway
from roborock_auto.config import Config


def _p(msg: str = "") -> None:
    print(msg, flush=True)


async def _section(name: str, coro):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - discovery should show, not crash
        _p(f"{name}: FAILED ({type(e).__name__}: {e})")
        return None


async def main() -> None:
    config = Config.from_env()
    gw = get_gateway(config)
    _p(f"gateway mode : {config.gateway}")
    await gw.connect()
    try:
        status = await _section("status", gw.get_status())
        if status:
            _p(f"status       : state={status.state_name} battery={status.battery} "
               f"docked={status.docked} in_cleaning={status.in_cleaning}")

        carpet = await _section("carpet", gw.get_carpet_mode())
        if carpet:
            _p(f"carpet mode  : {carpet.value}  "
               f"(toggle it in the app and re-run to capture both Rise/Avoid values)")

        rooms = await _section("rooms", gw.list_rooms())
        if rooms is not None:
            _p("rooms:")
            for room in rooms:
                _p(f"  {room.segment_id:>4}  {room.name}")

        routines = await _section("routines", gw.list_routines())
        if routines is not None:
            _p("routines (use the id for VACUUM_SCENE_ID / MOP_SCENE_ID):")
            for routine in routines:
                _p(f"  {routine.id:>8}  {routine.name}")

        cons = await _section("consumables", gw.get_consumables())
        if cons:
            _p(f"consumables  : brush={cons.main_brush_pct}% side={cons.side_brush_pct}% "
               f"filter={cons.filter_pct}%")
    finally:
        await gw.close()


if __name__ == "__main__":
    asyncio.run(main())
