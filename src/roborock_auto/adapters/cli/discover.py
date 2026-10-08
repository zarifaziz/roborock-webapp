"""Read-only dump of everything the automation needs to be configured.

    uv run python -m roborock_auto.adapters.cli.discover

Works in both modes. In live mode it needs ROBOROCK_EMAIL and a session from the login CLI.
Without ROBOROCK_DUID it lists the devices on the account and stops; with it, it prints the
robot's routines, rooms and raw carpet value so you can fill in .env. Nothing is written to
the device. Each section is independent: if one call fails on your firmware, the rest
still print.
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
    config = Config.from_env(require_device=False)
    gw = get_gateway(config)
    _p(f"gateway mode : {config.gateway}")
    if config.gateway == "live" and not config.duid:
        try:
            devices = await gw.list_devices()
        finally:
            await gw.close()
        _p("devices on this account:")
        for device in devices:
            _p(f"  {device.duid}  {device.name}  {device.model}")
        _p("Set ROBOROCK_DUID in .env to your robot's duid and run discover again.")
        return
    await gw.connect()
    try:
        status = await _section("status", gw.get_status())
        if status:
            _p(f"status       : state={status.state_name} battery={status.battery} "
               f"docked={status.docked} in_cleaning={status.in_cleaning}")

        carpet = await _section("carpet", gw.get_carpet_value())
        if carpet is not None:
            _p(f"carpet value : {carpet}  (set carpet Rise, then Avoid, in the Roborock app "
               f"and run discover after each to capture both values)")

        rooms = await _section("rooms", gw.list_rooms())
        if rooms is not None:
            _p("rooms:")
            for room in rooms:
                _p(f"  {room.segment_id:>4}  {room.name}")

        routines = await _section("routines", gw.list_routines())
        if routines is not None:
            _p("routines:")
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
