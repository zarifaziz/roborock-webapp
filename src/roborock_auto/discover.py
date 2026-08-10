"""Read-only dump of everything the automation needs to be configured.

    uv run python -m roborock_auto.discover

Works in both modes. In live mode it prints the real duid, routines, and current carpet
value so you can fill in .env. Nothing is written to the device.
"""

from __future__ import annotations

import asyncio

from roborock_auto.config import Config
from roborock_auto.gateway import get_gateway


async def main() -> None:
    config = Config.from_env()
    gw = get_gateway(config)
    await gw.connect()
    try:
        status = await gw.get_status()
        print(f"gateway mode : {config.gateway}")
        print(f"status       : state={status.state_name} battery={status.battery} "
              f"docked={status.docked} in_cleaning={status.in_cleaning}")
        print(f"carpet mode  : {(await gw.get_carpet_mode()).value}")
        print("rooms:")
        for room in await gw.list_rooms():
            print(f"  {room.segment_id:>4}  {room.name}")
        print("routines (use the id for VACUUM_SCENE_ID / MOP_SCENE_ID):")
        for routine in await gw.list_routines():
            print(f"  {routine.id:>6}  {routine.name}")
        cons = await gw.get_consumables()
        print(f"consumables  : brush={cons.main_brush_pct}% side={cons.side_brush_pct}% "
              f"filter={cons.filter_pct}%")
    finally:
        await gw.close()


if __name__ == "__main__":
    asyncio.run(main())
