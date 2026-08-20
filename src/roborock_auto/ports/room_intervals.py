"""Scheduling-context port: the user's standing intent about how often each Segment
should be cleaned.

Separate from the cleaning log, which records what happened: this store holds only the
intent, and the Overdue view joins the two. Persisted rows are scoped to one device
(docs/adr/0005).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import RoomInterval


@runtime_checkable
class RoomIntervalPort(Protocol):
    async def set_interval(self, interval: RoomInterval) -> None:
        """Set or replace one Segment's Room Interval."""
        ...

    async def list_intervals(self) -> list[RoomInterval]: ...

    async def remove_interval(self, segment_id: int) -> bool:
        """True when a Room Interval was there to remove."""
        ...
