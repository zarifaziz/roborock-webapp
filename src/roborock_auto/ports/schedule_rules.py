"""Scheduling-context port: the store of Schedule Rules, the user's standing intents.

Separate from the Room Interval store and the cleaning log: this store holds only Rules,
consumed by the schedules CRUD endpoints. Persisted rows are scoped to one device
(docs/adr/0005). The Action payload is parsed into its typed value object on read, so a
malformed row fails loudly at this boundary instead of leaking a dictionary into the
domain (docs/adr/0004).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import Action, ScheduleRule, WeeklyGrid


@runtime_checkable
class ScheduleRulePort(Protocol):
    async def create_rule(
        self, name: str, recurrence: WeeklyGrid, action: Action, enabled: bool
    ) -> ScheduleRule:
        """Store a new Rule and return it with its assigned identity."""
        ...

    async def list_rules(self) -> list[ScheduleRule]:
        """Every Rule that has not been deleted, in identity order."""
        ...

    async def replace_rule(self, rule: ScheduleRule) -> bool:
        """Replace the stored Rule with `rule.id` wholesale. True when it was there."""
        ...

    async def delete_rule(self, rule_id: int) -> bool:
        """Soft-delete one Rule, so its history survives. True when it was there."""
        ...
