"""The RunSlot: at most one long-running run (a Sequence, or the Two-Stage Clean preset)
drives the robot at a time.

One instance per process, shared by every entry point that starts a run and by the
Scheduler, so a run started anywhere is visible and cancellable everywhere. acquire is
synchronous on purpose: with no await between checking and taking the slot, two callers
on the same event loop can never both win it.
"""

from __future__ import annotations

import asyncio
from typing import Any


class RunSlot:
    def __init__(self) -> None:
        self._held = False
        # Written by whoever holds the slot, read by status endpoints. It outlives the
        # hold, so the last run's outcome stays readable after it releases.
        self.progress: dict[str, Any] = {}
        self.cancel = asyncio.Event()
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        """Take the slot, or return False if a run already holds it. Each run gets a
        fresh cancel event, so a cancel aimed at the last run cannot stop the next."""
        if self._held:
            return False
        self._held = True
        self.cancel = asyncio.Event()
        self.task = None
        return True

    def release(self) -> None:
        self._held = False
