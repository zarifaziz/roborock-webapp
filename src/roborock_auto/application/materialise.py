"""The Materialiser: turns Schedule Rules into Occurrence records over a forward horizon.

It asks each enabled Rule's Recurrence for the instants it implies within the window and
writes one PENDING Occurrence per instant (docs/adr/0002). The store's unique key makes
the whole pass idempotent, so it is safe to run unconditionally — after any Rule change
and on every scheduler tick — and self-heals a horizon that ran dry.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from roborock_auto.ports.occurrences import OccurrencePort
from roborock_auto.ports.schedule_rules import ScheduleRulePort

MATERIALISATION_HORIZON = timedelta(days=14)
# Applied when the Occurrence is written, then read back from the record: each Occurrence
# carries its own terms, so a per-Rule override is later additive.
DEFAULT_DEADLINE_AFTER = timedelta(hours=6)


async def materialise(
    rules: ScheduleRulePort,
    occurrences: OccurrencePort,
    *,
    now: datetime,
    tz: tzinfo,
) -> None:
    for rule in await rules.list_rules():
        if not rule.enabled:
            continue
        for instant in rule.recurrence.occurrences_between(
            now, now + MATERIALISATION_HORIZON, tz
        ):
            await occurrences.add_pending(
                rule.id, scheduled_for=instant, deadline=instant + DEFAULT_DEADLINE_AFTER
            )
