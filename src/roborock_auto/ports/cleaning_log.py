"""Control-context port: the durable log of every Clean this app starts.

Writes come from the clean use cases; the per-Segment last-cleaned read is derived from
the recorded runs, never stored as a summary (docs/adr/0003). Persisted rows are scoped
to one device (docs/adr/0005).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import CleaningRun


@runtime_checkable
class CleaningLogPort(Protocol):
    async def record_run(self, run: CleaningRun) -> None: ...

    async def last_cleaned(self) -> dict[int, datetime]:
        """Most recent started_at per segment, over runs that name their segments."""
        ...

    async def list_runs(self) -> list[CleaningRun]: ...
