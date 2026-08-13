"""In-memory auth double: records calls and succeeds with no network."""

from __future__ import annotations


class FakeAuth:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def request_code(self, email: str) -> None:
        self.calls.append(("request_code", email))

    async def submit_code(self, email: str, code: str) -> None:
        self.calls.append(("submit_code", email, code))
