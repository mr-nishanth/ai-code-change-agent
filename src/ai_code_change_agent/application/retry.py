"""Retry a whole use-case method after a lost optimistic race (DYNAMODB-DATA-MODEL.md 5.1).

A unit of work that loses an optimistic race raises `ConcurrentModificationError` (an item it read
changed before it committed). The decorated method runs again from the start, in a new unit of work
with fresh reads, up to `RETRY_ATTEMPTS` times with full jitter; this reproduces waiting for the
row lock. Genuine conflicts carry their own error and are never retried. The service supplies the
sleep (`self._sleep`) so tests run without waiting."""

import functools
import random
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate, Protocol

from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError

Sleep = Callable[[float], Awaitable[None]]

RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 0.025
RETRY_MAX_DELAY = 0.5


class RetryingService(Protocol):
    _sleep: Sleep


def retry_delay(retry: int) -> float:
    """Full jitter: U(0, min(max, base * 2**retry))."""
    cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * float(2**retry))
    return random.random() * cap  # noqa: S311 - retry jitter, not a secret


def retry_concurrent[Svc: RetryingService, **P, T](
    method: Callable[Concatenate[Svc, P], Coroutine[Any, Any, T]],
) -> Callable[Concatenate[Svc, P], Coroutine[Any, Any, T]]:
    """Run the whole method again (new unit of work, fresh reads) after a lost optimistic race."""

    @functools.wraps(method)
    async def wrapper(service: Svc, /, *args: P.args, **kwargs: P.kwargs) -> T:
        for retry in range(RETRY_ATTEMPTS):
            try:
                return await method(service, *args, **kwargs)
            except ConcurrentModificationError:
                if retry + 1 >= RETRY_ATTEMPTS:
                    raise
            await service._sleep(retry_delay(retry))
        raise AssertionError("unreachable")  # pragma: no cover

    return wrapper
