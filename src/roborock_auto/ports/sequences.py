"""Control-context port: the store of Sequences, the user's saved multi-Step cleans.

Consumed by the Sequences CRUD endpoints and by the scheduler, which looks up the
Sequence a Sequence Action names. Persisted rows are scoped to one device
(docs/adr/0005). Steps are parsed into their typed value objects on read, so a malformed
row fails loudly at this boundary instead of leaking a dictionary into the domain
(docs/adr/0004).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import Sequence, Step


@runtime_checkable
class SequencePort(Protocol):
    async def create_sequence(self, name: str, steps: tuple[Step, ...]) -> Sequence:
        """Store a new Sequence and return it with its assigned identity."""
        ...

    async def list_sequences(self) -> list[Sequence]:
        """Every Sequence, in identity order."""
        ...

    async def get_sequence(self, sequence_id: int) -> Sequence | None:
        """One Sequence, or None when there is no such Sequence."""
        ...

    async def replace_sequence(self, sequence: Sequence) -> bool:
        """Replace the stored Sequence with `sequence.id` wholesale. True when it was
        there."""
        ...

    async def delete_sequence(self, sequence_id: int) -> bool:
        """Delete one Sequence. Its id is never reused. True when it was there."""
        ...
