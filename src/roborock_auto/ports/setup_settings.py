"""Control-context port: the user's Setup choice of fan power, water flow and mop route.

The robot cannot be the record of it: every Clean's mode switch overwrites all three, and
a vacuum-only or mop-only Clean leaves the robot with no water flow or no suction to read
back. Written by the Setup endpoints, read by every Clean before its mode switch.
Persisted per device (docs/adr/0005).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import CleanSettings


@runtime_checkable
class SetupSettingsPort(Protocol):
    async def get_setup_settings(self) -> CleanSettings:
        """The stored choice; a field the user never chose is None."""
        ...

    async def save_setup_settings(self, settings: CleanSettings) -> None:
        """Store each field that is set; a None field keeps its stored value."""
        ...
