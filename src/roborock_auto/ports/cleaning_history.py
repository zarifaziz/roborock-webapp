"""Read-only robot cleaning history."""

from typing import Protocol

from roborock_auto.domain.cleaning_history import CleanRecord


class CleaningHistoryPort(Protocol):
    async def get_last_clean_record(self) -> CleanRecord | None: ...
