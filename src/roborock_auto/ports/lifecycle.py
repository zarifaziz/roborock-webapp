"""Connection lifecycle, separate from what a connected robot can do."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ConnectionPort(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
