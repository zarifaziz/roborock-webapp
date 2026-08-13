"""Auth seam: the account login flow the driving web adapter needs the world to honor.

Kept separate from the device ports: logging in is a cloud-account concern, not a robot
command, and only the live adapter touches the network here."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthPort(Protocol):
    async def request_code(self, email: str) -> None: ...
    async def submit_code(self, email: str, code: str) -> None: ...
