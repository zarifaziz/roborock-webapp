"""Scheduling-context port: the store of Occurrences, the durable firings of Schedule
Rules.

Separate from the Rule store, which holds the standing intent: this store holds what that
intent implies at specific instants, consumed by the scheduler and the timeline endpoint.
Persisted rows are scoped to one device (docs/adr/0005). Firing is a claim, not a
decision (docs/adr/0002): a conditional update of a PENDING record to CLAIMED, where
affecting exactly one row is the right to fire, so concurrent claimers are safe rather
than forbidden. Occurrence history is never pruned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import Occurrence


@runtime_checkable
class OccurrencePort(Protocol):
    async def add_pending(
        self, rule_id: int, scheduled_for: datetime, deadline: datetime
    ) -> None:
        """Store one PENDING Occurrence. A live row already at (rule_id, scheduled_for)
        makes this a no-op, so Materialisation is idempotent and safe to run
        unconditionally."""
        ...

    async def claim(self, occurrence_id: int) -> bool:
        """Conditionally move one PENDING Occurrence to CLAIMED. True exactly when this
        caller flipped it — the right to fire; a concurrent claimer sees False."""
        ...

    async def cancel_pending(self, rule_id: int) -> None:
        """Mark every PENDING Occurrence of one Rule CANCELLED, including ones already
        past their scheduled instant — a restated intent supersedes the old one. A
        CLAIMED Occurrence is left alone: commands are in flight."""
        ...

    async def list_occurrences(self) -> list[Occurrence]:
        """Every Occurrence ever recorded, earliest scheduled first — the timeline."""
        ...
