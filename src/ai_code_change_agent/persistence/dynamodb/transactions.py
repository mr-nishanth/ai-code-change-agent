"""Transactions and the unit-of-work base for the DynamoDB adapters (DYNAMODB-DATA-MODEL.md 5, 5.1).

`TxBuilder` collects at most 100 `TransactWriteItems` entries (one per item; DynamoDB rejects two
operations on one item, so the builder refuses them first) and sends them with a
`ClientRequestToken`: a retry of the same commit (by this adapter or botocore) is idempotent, and
the same token with another body is `IdempotentParameterMismatchException`. A cancelled transaction
writes nothing; the first `ConditionalCheckFailed` reason is mapped by its item index to that item's
`on_failed` error (default `ConcurrentModificationError`).

The pattern Tasks 4-8 follow: `DynamoUnitOfWork`
-------------------------------------------------
An adapter's unit of work (`DynamoRegistryUnitOfWork`, `DynamoChangeUnitOfWork`, ...) subclasses
`DynamoUnitOfWork` and keeps the port it implements (`async with uow: ...; await uow.commit()`;
leaving the block without `commit()` discards everything, like the SQL adapters' rollback).

1. **Read** with `await self.get(key)` (or `self.query(pk, prefix)`): a strongly consistent read
   whose snapshot, including `row_version`, is remembered for the rest of the unit of work. Pass
   `depend=False` for reads no decision depends on (display data).
2. **Write** into a buffer: `create(item)` for a new item or guard (`attribute_not_exists(PK)`,
   with `on_exists` naming the domain error, e.g. `ConflictError("alias_taken")`);
   `replace(item)` for a mutable item already read (`row_version` becomes seen + 1, condition
   `row_version = :seen`, plus any extra condition such as "attempt not terminal");
   `delete(key)` for an item already read; `check(...)` for an explicit `ConditionCheck` on an item
   not written (e.g. "profile version is published"). Reads after a write see the buffered state
   (**read-your-writes**), so `add_repository` then an audit event, or `_bump` returning the new
   row, work as with SQL.
3. **Commit** sends one transaction: every buffered write, plus a `ConditionCheck` for every item
   read with `depend=True` and not written (`row_version = :seen`, or `attribute_not_exists(PK)` for
   an item that was absent; immutable items without `row_version` need none). Any item that changed
   since it was read cancels the whole transaction with `ConcurrentModificationError`, which the
   service retries as a whole method (bounded, jittered); genuine conflicts carry their own error.

Mutable entities therefore always carry `row_version` (N), set to 1 by the creator. The largest
transaction in the design is the multi-repository create (42 items), well under the limit of 100."""

import copy
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from ai_code_change_agent.persistence.dynamodb.codec import decode_int
from ai_code_change_agent.persistence.dynamodb.errors import (
    Failure,
    Item,
    TransactionTooLargeError,
)
from ai_code_change_agent.persistence.dynamodb.keys import Key
from ai_code_change_agent.persistence.dynamodb.table import (
    Table,
    expression_args,
    serialize_item,
)

MAX_TRANSACTION_ITEMS = 100
VERSION = "row_version"
_VERSION_NAMES = {"#row_version": VERSION}
_NOT_EXISTS = "attribute_not_exists(PK)"
_EXISTS = "attribute_exists(PK)"


@dataclass(frozen=True)
class _Entry:
    key: Key
    body: dict[str, Any]
    failure: Failure | None


class TxBuilder:
    def __init__(self, table: Table, token: str | None = None) -> None:
        self._table = table
        self.token = token or str(uuid.uuid4())
        if not 1 <= len(self.token) <= 36:
            raise ValueError("ClientRequestToken must be 1..36 characters")
        self._entries: list[_Entry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def put(
        self,
        item: Mapping[str, Any],
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> Self:
        body = {"TableName": self._table.name, "Item": serialize_item(item)}
        body |= expression_args(condition=condition, names=names, values=values)
        return self._add("Put", Key.of(dict(item)), body, on_failed)

    def update(
        self,
        key: Key,
        *,
        update: str,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> Self:
        body = {
            "TableName": self._table.name,
            "Key": serialize_item(key.as_dict()),
            "UpdateExpression": update,
        }
        body |= expression_args(condition=condition, names=names, values=values)
        return self._add("Update", key, body, on_failed)

    def delete(
        self,
        key: Key,
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> Self:
        body = {"TableName": self._table.name, "Key": serialize_item(key.as_dict())}
        body |= expression_args(condition=condition, names=names, values=values)
        return self._add("Delete", key, body, on_failed)

    def condition_check(
        self,
        key: Key,
        *,
        condition: str,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> Self:
        body = {"TableName": self._table.name, "Key": serialize_item(key.as_dict())}
        body |= expression_args(condition=condition, names=names, values=values)
        return self._add("ConditionCheck", key, body, on_failed)

    def _add(self, operation: str, key: Key, body: dict[str, Any], failure: Failure | None) -> Self:
        if len(self._entries) >= MAX_TRANSACTION_ITEMS:
            raise TransactionTooLargeError()
        if any(entry.key == key for entry in self._entries):
            raise ValueError("a transaction may contain one operation per item")
        if failure is not None and not isinstance(failure, BaseException):
            body["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"
        self._entries.append(_Entry(key, {operation: body}, failure))
        return self

    def failure_at(self, index: int) -> Failure | None:
        if 0 <= index < len(self._entries):
            return self._entries[index].failure
        return None

    def request(self) -> dict[str, Any]:
        if not self._entries:
            raise ValueError("empty transaction")
        if len(self._entries) > MAX_TRANSACTION_ITEMS:  # pragma: no cover - refused in _add
            raise TransactionTooLargeError()
        return {
            "TransactItems": [entry.body for entry in self._entries],
            "ClientRequestToken": self.token,
        }

    async def commit(self) -> None:
        await self._table.transact_write(self)


@dataclass
class _Write:
    kind: str  # "create" | "replace" | "delete"
    item: Item | None  # None for delete
    condition: str
    names: dict[str, str]
    values: dict[str, Any]
    failure: Failure | None


def _and(*conditions: str | None) -> str:
    parts = [c for c in conditions if c]
    return " AND ".join(f"({c})" for c in parts) if len(parts) > 1 else parts[0]


class DynamoUnitOfWork:
    """Optimistic unit of work over one table; see the module docstring for the pattern."""

    def __init__(self, table: Table) -> None:
        self.table = table
        self._reset()

    def _reset(self) -> None:
        self._read: dict[Key, Item | None] = {}
        self._depends: set[Key] = set()
        self._writes: dict[Key, _Write] = {}
        self._checks: list[tuple[Key, str, dict[str, str], dict[str, Any], Failure | None]] = []

    async def __aenter__(self) -> Self:
        self._reset()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._reset()  # anything not committed is discarded

    # Reads ---------------------------------------------------------------------------------

    def _view(self, key: Key) -> tuple[bool, Item | None]:
        """(known, item) from the overlay of buffered writes."""
        write = self._writes.get(key)
        if write is None:
            return False, None
        return True, copy.deepcopy(write.item)

    async def get(self, key: Key, *, depend: bool = True) -> Item | None:
        known, item = self._view(key)
        if known:
            return item
        if key not in self._read:
            self._read[key] = await self.table.get(key, consistent=True)
        if depend:
            self._depends.add(key)
        return copy.deepcopy(self._read[key])

    async def query(
        self, pk: str, sk_prefix: str | None = None, *, depend: bool = False
    ) -> list[Item]:
        """The item collection `pk` (optionally `begins_with(SK, sk_prefix)`), consistent, with
        buffered writes applied, sorted by SK."""
        condition = "PK = :pk"
        values: dict[str, Any] = {":pk": pk}
        if sk_prefix:
            condition += " AND begins_with(SK, :prefix)"
            values[":prefix"] = sk_prefix
        found: dict[Key, Item | None] = {}
        for item in await self.table.query(key_condition=condition, values=values):
            key = Key.of(item)
            self._read.setdefault(key, item)  # the first snapshot wins (repeatable read)
            found[key] = self._read[key]
            if depend:
                self._depends.add(key)
        for key, write in self._writes.items():
            if key.pk == pk and (not sk_prefix or key.sk.startswith(sk_prefix)):
                found[key] = write.item
        return [copy.deepcopy(row) for key in sorted(found) if (row := found[key]) is not None]

    def seen_version(self, key: Key) -> int | None:
        item = self._read.get(key)
        if item is None or VERSION not in item:
            return None
        return decode_int(item[VERSION], VERSION)

    # Buffered writes -----------------------------------------------------------------------

    def create(self, item: Mapping[str, Any], *, on_exists: Failure | None = None) -> Item:
        """A new item (`attribute_not_exists(PK)`); `on_exists` is the error if it exists."""
        key = Key.of(dict(item))
        if key in self._writes:
            raise ValueError("item already written in this unit of work")
        body = copy.deepcopy(dict(item))
        self._writes[key] = _Write("create", body, _NOT_EXISTS, {}, {}, on_exists)
        return copy.deepcopy(body)

    def replace(
        self,
        item: Mapping[str, Any],
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> Item:
        """Replace a versioned item read (or created) in this unit of work; returns the new row."""
        key = Key.of(dict(item))
        body = copy.deepcopy(dict(item))
        pending = self._writes.get(key)
        if pending is not None:
            if pending.item is None:
                raise ValueError("item deleted in this unit of work")
            # One write per item and commit: keep the first write's condition and version.
            if VERSION in pending.item:
                body[VERSION] = pending.item[VERSION]
            pending.item = body
            return copy.deepcopy(body)
        seen = self.seen_version(key)
        if seen is None:
            raise ValueError("replace needs a versioned item read in this unit of work")
        body[VERSION] = seen + 1
        self._writes[key] = _Write(
            "replace",
            body,
            _and("#row_version = :row_version_seen", condition),
            {**_VERSION_NAMES, **dict(names or {})},
            {":row_version_seen": seen, **dict(values or {})},
            on_failed,
        )
        return copy.deepcopy(body)

    def delete(
        self,
        key: Key,
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> None:
        """Delete an item read (or created) in this unit of work."""
        pending = self._writes.get(key)
        if pending is not None and pending.kind == "create":
            del self._writes[key]  # created and deleted again: nothing to send
            return
        if pending is not None:
            raise ValueError("item already written in this unit of work")
        if self._read.get(key) is None:
            raise ValueError("delete needs an item read in this unit of work")
        seen = self.seen_version(key)
        if seen is None:
            base, base_names, base_values = _EXISTS, {}, {}
        else:
            base = "#row_version = :row_version_seen"
            base_names, base_values = dict(_VERSION_NAMES), {":row_version_seen": seen}
        self._writes[key] = _Write(
            "delete",
            None,
            _and(base, condition),
            {**base_names, **dict(names or {})},
            {**base_values, **dict(values or {})},
            on_failed,
        )

    def check(
        self,
        key: Key,
        *,
        condition: str,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> None:
        """An explicit `ConditionCheck` on an item this unit of work does not write."""
        self._checks.append((key, condition, dict(names or {}), dict(values or {}), on_failed))

    # Commit --------------------------------------------------------------------------------

    def build(self, token: str | None = None) -> TxBuilder:
        tx = self.table.transaction(token)
        for key, write in self._writes.items():
            if write.item is None:
                tx.delete(
                    key,
                    condition=write.condition,
                    names=write.names,
                    values=write.values,
                    on_failed=write.failure,
                )
            else:
                tx.put(
                    write.item,
                    condition=write.condition,
                    names=write.names,
                    values=write.values,
                    on_failed=write.failure,
                )
        explicit = {key for key, *_ in self._checks}
        for key in sorted(self._depends - self._writes.keys() - explicit):
            snapshot = self._read.get(key)
            if snapshot is None:
                tx.condition_check(key, condition=_NOT_EXISTS)
            elif VERSION in snapshot:
                tx.condition_check(
                    key,
                    condition="#row_version = :row_version_seen",
                    names=_VERSION_NAMES,
                    values={":row_version_seen": self.seen_version(key)},
                )
        for key, condition, names, values, failure in self._checks:
            tx.condition_check(
                key, condition=condition, names=names, values=values, on_failed=failure
            )
        return tx

    async def commit(self) -> None:
        tx = self.build()
        if len(tx):
            await tx.commit()
        self._reset()
