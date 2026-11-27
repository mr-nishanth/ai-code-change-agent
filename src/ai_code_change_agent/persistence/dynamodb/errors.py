"""Error mapping for the DynamoDB adapters. Driver text never leaves this package.

Every botocore failure becomes a domain error whose message is a fixed code or an AWS error code
(for example `ThrottlingException`), never the service message, request id or item values. The
original exception is not chained (`raise ... from None`) so a logged traceback cannot carry it.

Retry (bounded, full jitter): `TransactionConflict` (on a transaction, as a cancellation reason, or
`TransactionConflictException` on a single-item write), `ThrottlingException`,
`ProvisionedThroughputExceededException` and `RequestLimitExceeded`. When retries run out a lost
conflict is `ConcurrentModificationError` and throttling is `PersistenceError`. Floci serialises
transactions and never throttles (DYNAMODB-DATA-MODEL.md 10.4), so botocore `Stubber` tests cover
these paths."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.webhooks.models import PersistenceError

__all__ = [
    "ConcurrentModificationError",
    "CorruptItemError",
    "DocumentTooLargeError",
    "PersistenceError",
    "TransactionTooLargeError",
]

Item = dict[str, Any]
# What a failed condition becomes: an exception, or a factory given the item's old image (when
# DynamoDB returned one) so a caller can, for example, read the request id off a guard item.
Failure = BaseException | Callable[[Item | None], BaseException]

THROTTLE_CODES = frozenset(
    {"ThrottlingException", "ProvisionedThroughputExceededException", "RequestLimitExceeded"}
)
CONFLICT_CODES = frozenset({"TransactionConflictException"})
THROTTLE_REASONS = frozenset({"ThrottlingError", "ProvisionedThroughputExceeded"})


class DocumentTooLargeError(PersistenceError):
    """A JSON document is over the adapter's 300 KB limit (the DynamoDB item limit is 400 KB)."""

    def __init__(self) -> None:
        super().__init__("document_too_large")


class CorruptItemError(PersistenceError):
    """A stored attribute does not decode. The message names the attribute only."""

    def __init__(self, attribute: str) -> None:
        super().__init__(f"corrupt_item:{attribute}")


class TransactionTooLargeError(PersistenceError):
    """More than 100 items in one TransactWriteItems; refused before any call."""

    def __init__(self) -> None:
        super().__init__("transaction_too_large")


class _Kind(Enum):
    CONDITION = "condition"
    CONFLICT = "conflict"
    THROTTLE = "throttle"
    OTHER = "other"


@dataclass(frozen=True)
class Classified:
    kind: _Kind
    code: str
    index: int | None = None  # the first failing item of a cancelled transaction
    old_item: Mapping[str, Any] | None = None  # raw (typed) image, when DynamoDB returned one

    @property
    def is_condition(self) -> bool:
        return self.kind is _Kind.CONDITION

    @property
    def retryable(self) -> bool:
        return self.kind in (_Kind.CONFLICT, _Kind.THROTTLE)


def classify(exc: ClientError) -> Classified:
    error = exc.response.get("Error", {}) if isinstance(exc.response, Mapping) else {}
    code = str(error.get("Code") or "ClientError")
    if code == "ConditionalCheckFailedException":
        return Classified(_Kind.CONDITION, code, None, exc.response.get("Item"))
    if code == "TransactionCanceledException":
        return _classify_cancellation(code, exc.response.get("CancellationReasons") or [])
    if code in CONFLICT_CODES:
        return Classified(_Kind.CONFLICT, code)
    if code in THROTTLE_CODES:
        return Classified(_Kind.THROTTLE, code)
    return Classified(_Kind.OTHER, code)


def _classify_cancellation(code: str, reasons: Sequence[Mapping[str, Any]]) -> Classified:
    codes = [str((reason or {}).get("Code") or "None") for reason in reasons]
    for index, reason_code in enumerate(codes):
        if reason_code == "ConditionalCheckFailed":
            return Classified(_Kind.CONDITION, code, index, reasons[index].get("Item"))
    if "TransactionConflict" in codes:
        return Classified(_Kind.CONFLICT, code)
    if THROTTLE_REASONS & set(codes):
        return Classified(_Kind.THROTTLE, code)
    detail = next((c for c in codes if c != "None"), None)
    return Classified(_Kind.OTHER, f"{code}:{detail}" if detail else code)


def resolve_failure(failure: Failure | None, old_item: Item | None) -> BaseException:
    if failure is None:
        return ConcurrentModificationError()
    if isinstance(failure, BaseException):
        return failure
    return failure(old_item)


def terminal_error(classified: Classified) -> PersistenceError | ConcurrentModificationError:
    """The error for a non-condition failure once retries are exhausted (or not allowed)."""
    if classified.kind is _Kind.CONFLICT:
        return ConcurrentModificationError()
    return PersistenceError(classified.code)


def transport_error(exc: BotoCoreError) -> PersistenceError:
    """Connection, timeout and parameter errors raised by botocore itself."""
    return PersistenceError(type(exc).__name__)


def _system_random() -> float:
    return random.random()  # noqa: S311 - retry jitter, not a secret


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry with full jitter: delay = U(0, min(max_delay, base_delay * 2**n))."""

    attempts: int = 5
    base_delay: float = 0.025
    max_delay: float = 0.5
    sleep: Callable[[float], Awaitable[None]] = field(default=_default_sleep, repr=False)
    random: Callable[[], float] = field(default=_system_random, repr=False)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")

    def delay(self, retry: int) -> float:
        return self.random() * min(self.max_delay, self.base_delay * float(2**retry))
