"""Maintenance-context port. Dock actions (dust collection, mop wash, drying) land here
as they are implemented."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import Consumables


@runtime_checkable
class MaintenancePort(Protocol):
    async def get_consumables(self) -> Consumables: ...
