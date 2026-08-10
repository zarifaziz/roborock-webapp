"""Cron entrypoint: run the carpet-aware two-stage clean once and exit non-zero on failure.

    uv run python -m roborock_auto.run
"""

from __future__ import annotations

import asyncio
import sys

from roborock_auto.automation import AutomationError, run_two_stage
from roborock_auto.config import Config
from roborock_auto.gateway import get_gateway


async def main() -> int:
    config = Config.from_env()
    gw = get_gateway(config)
    await gw.connect()
    try:
        await run_two_stage(gw, config)
    except AutomationError as e:
        print(f"automation aborted: {e}", file=sys.stderr)
        return 1
    finally:
        await gw.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
