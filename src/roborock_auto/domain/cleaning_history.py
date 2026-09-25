"""Observed robot history, distinct from this app's requested cleaning runs."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CleanRecord:
    started_at: datetime | None
    ended_at: datetime | None
    duration_s: int | None
    area_m2: float | None
    outcome: str
    error_code: int | None
