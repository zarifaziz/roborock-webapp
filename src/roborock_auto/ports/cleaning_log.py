"""Control-context port: the durable log of every Clean this app starts.

Writes come from the clean use cases; the per-Segment last-cleaned read is derived from
the recorded runs, never stored as a summary (docs/adr/0003). Persisted rows are scoped
to one device (docs/adr/0005).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import CleaningRun


@runtime_checkable
class CleaningLogPort(Protocol):
    async def record_run(self, run: CleaningRun) -> None: ...

    async def last_cleaned(self, segment_ids: Iterable[int]) -> dict[int, datetime]:
        """Most recent started_at for each requested Segment. A whole-house run names
        no Segments, so it counts towards every requested one; a Segment never cleaned
        is absent from the result."""
        ...

    async def list_runs(self) -> list[CleaningRun]: ...
