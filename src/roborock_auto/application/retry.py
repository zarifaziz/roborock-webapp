"""Workflow-level retry: should this step be reattempted at all?

Transport-level retry (serialize, back off on a flaky cloud call) lives in the adapter,
which owns the connection. This helper is for use-case steps where a failure is worth a
few more tries before the whole workflow aborts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

Sleep = Callable[[float], Awaitable[None]]


class AutomationError(Exception):
    pass


class DepartRejected(AutomationError):
    """The robot settled idle on the dock without leaving — the clean trigger did not take.
    A typed signal so the caller can re-issue the trigger without matching on message text."""


async def with_retry(
    fn: Callable[[], Awaitable[None]],
    *,
    sleep: Sleep,
    attempts: int = 3,
    base_delay: float = 2.0,
) -> None:
    for i in range(attempts):
        try:
            await fn()
            return
        except AutomationError:
            raise
        except Exception:
            if i == attempts - 1:
                raise
            await sleep(base_delay * (2**i))
