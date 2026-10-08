"""What the discover CLI needs to fill in .env before live mode can start."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from roborock_auto.domain.models import AccountDevice


@runtime_checkable
class OnboardingPort(Protocol):
    async def list_devices(self) -> list[AccountDevice]:
        """Every device on the account. Works before connect() and without a duid."""
        ...

    async def get_carpet_value(self) -> int:
        """The raw carpet setting, an opaque number the user copies into
        CARPET_RISE_VALUE / CARPET_AVOID_VALUE. Nothing else may interpret it."""
        ...
