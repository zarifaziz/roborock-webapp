"""Cron entrypoint: run the carpet-aware two-stage clean once and exit non-zero on failure.

    uv run python -m roborock_auto.adapters.cron.run
"""

from __future__ import annotations

import asyncio
import sys

from roborock_auto.adapters import get_gateway
from roborock_auto.application.retry import AutomationError
from roborock_auto.application.two_stage_clean import run_two_stage
from roborock_auto.config import Config


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
