"""DynamoDB usage ledger and cost queries (DYNAMODB-ACCESS-PATTERNS.md 6, U1-U5).

`DynamoUsageStore` implements both the `UsageStore` port (`add`) and the `CostQuery` port (`rows`).

U1 `add`: one `TransactWriteItems` of up to 4 items, every one `attribute_not_exists(PK)`:
- `LLMCALL#<call_id>/GUARD`: the call (LangChain run) id is unique;
- `LLMMSG#<provider_message_id>/GUARD`: only when the provider reported a message id;
- `USAGE#ATT#<aid>/<created_at>#<call_id>`: the attempt copy, when the call belongs to an attempt;
- `USAGE#SES#<session_id>/<created_at>#<call_id>`: the session copy.
A failed condition on any item cancels the whole transaction and `add` returns False: a replayed
call id or provider message id records nothing. Rows are immutable (no update or delete path).
`cost_usd` is an exact decimal string with ten decimal places (rounded half-up, as the
`Numeric(16, 10)` column stored it); token counts are numbers, absent when usage is unknown.

U2-U4 `rows`: the ledger copies of one attempt (U2, consistent query per attempt) or one session
(U4, consistent query), or the attempts of a Story (U3: GSI1 `ISSUEKEY#<key>` -> request ids from
the index keys alone (GSI1 projects keys only), then a consistent query of each request's `ATT#`
items). Each row is joined to its attempt (C4: number and
current status), request (C1: Jira issue key) and repository (R4: key) with batched, strongly
consistent `BatchGetItem` reads, each item read at most once per call. Filters combine with AND,
rows come back ordered by (created_at, call id), as the SQL query did. U5 (no filter, the whole
ledger) has no caller and is refused with `ValueError`."""

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.report import CostRow
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.change_store import AttemptPointers
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_decimal,
    decode_int,
    decode_timestamp,
    decode_uuid,
    encode_decimal,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import CorruptItemError, Item
from ai_code_change_agent.persistence.dynamodb.table import Table

GSI1 = "GSI1"
_COST_QUANTUM = Decimal("1E-10")  # Numeric(16, 10)
_TOKENS = (
    "input_tokens",
    "output_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
)
_OPTIONAL_TEXT = ("provider_message_id", "inference_geo", "error_code")


class _AlreadyRecorded(Exception):  # noqa: N818 - internal signal, not an error
    """A guard or copy of this call exists: the call was recorded before."""


def _cost_text(value: Decimal) -> str:
    return encode_decimal(value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP))


def _str(item: Item, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        raise CorruptItemError(name)
    return value


def _opt_str(item: Item, name: str) -> str | None:
    return _str(item, name) if name in item else None


def _bool(item: Item, name: str) -> bool:
    value = item.get(name)
    if not isinstance(value, bool):
        raise CorruptItemError(name)
    return value


def usage_item(record: LlmCallRecord, pk: str) -> Item:
    """One immutable ledger copy (attempt or session partition)."""
    created = encode_timestamp(record.created_at)
    usage = record.usage
    item: Item = {
        "PK": pk,
        "SK": f"{created}{keys.SEP}{record.call_id}",
        "entity": "llm_usage",
        "call_id": str(record.call_id),
        "session_id": str(record.session_id),
        "stage": record.stage[:32],
        "provider": record.provider[:32],
        "model": record.model[:128],
        "succeeded": record.succeeded,
        "long_context": record.long_context,
        "pricing_version": record.pricing_version[:64],
        "created_at": created,
    }
    if record.attempt_id is not None:
        item["attempt_id"] = str(record.attempt_id)
    for name in _OPTIONAL_TEXT:
        value = getattr(record, name)
        if value is not None:
            item[name] = value
    if usage is not None:
        item["input_tokens"] = usage.input_tokens
        item["output_tokens"] = usage.output_tokens
        item["cache_write_5m_tokens"] = usage.cache_write_5m_tokens
        item["cache_write_1h_tokens"] = usage.cache_write_1h_tokens
        item["cache_read_tokens"] = usage.cache_read_tokens
        if record.cost_usd is not None:
            item["cost_usd"] = _cost_text(record.cost_usd)
    return item


def _record(item: Item) -> LlmCallRecord:
    usage = (
        TokenUsage(*(decode_int(item.get(name), name) for name in _TOKENS))
        if "input_tokens" in item
        else None
    )
    return LlmCallRecord(
        call_id=decode_uuid(item.get("call_id"), "call_id"),
        session_id=decode_uuid(item.get("session_id"), "session_id"),
        attempt_id=decode_uuid(item["attempt_id"], "attempt_id") if "attempt_id" in item else None,
        stage=_str(item, "stage"),
        provider=_str(item, "provider"),
        model=_str(item, "model"),
        succeeded=_bool(item, "succeeded"),
        usage=usage,
        cost_usd=decode_decimal(item["cost_usd"], "cost_usd") if "cost_usd" in item else None,
        pricing_version=_str(item, "pricing_version"),
        provider_message_id=_opt_str(item, "provider_message_id"),
        long_context=_bool(item, "long_context"),
        inference_geo=_opt_str(item, "inference_geo"),
        error_code=_opt_str(item, "error_code"),
        created_at=decode_timestamp(item.get("created_at"), "created_at"),
    )


@dataclass(frozen=True)
class _AttemptInfo:
    request_id: uuid.UUID
    number: int
    status: str
    repository_id: uuid.UUID


class _Joins:
    """The attempt, request and repository behind ledger rows, each read at most once per call."""

    def __init__(self, table: Table, pointers: AttemptPointers) -> None:
        self._table = table
        self._pointers = pointers
        self.attempts: dict[uuid.UUID, _AttemptInfo | None] = {}
        self.issue_keys: dict[uuid.UUID, str | None] = {}  # request id -> Jira issue key
        self.repositories: dict[uuid.UUID, str | None] = {}  # repository id -> key

    def add_attempt(self, item: Item) -> uuid.UUID:
        attempt_id = decode_uuid(item.get("id"), "id")
        self.attempts[attempt_id] = _AttemptInfo(
            request_id=decode_uuid(item.get("request_id"), "request_id"),
            number=decode_int(item.get("number"), "number"),
            status=_str(item, "status"),
            repository_id=decode_uuid(item.get("repository_id"), "repository_id"),
        )
        return attempt_id

    def add_request(self, item: Item) -> None:
        self.issue_keys[decode_uuid(item.get("id"), "id")] = _str(item, "jira_issue_key")

    async def issue_attempts(self, issue_keys: Iterable[str]) -> list[uuid.UUID]:
        """U3: the attempts of every request with one of these Jira issue keys."""
        unique = list(dict.fromkeys(issue_keys))
        requests = await asyncio.gather(
            *(
                self._table.query(
                    key_condition="GSI1PK = :pk",
                    values={":pk": keys.issue_key_gsi1pk(key)},
                    index=GSI1,
                )
                for key in unique
            )
        )
        request_ids: list[uuid.UUID] = []
        for issue_key, items in zip(unique, requests, strict=True):
            for item in items:  # keys only: `REQ#<rid>` / `REQUEST`, GSI1PK = this issue key
                pk = item.get("PK")
                if item.get("SK") != keys.SK_REQUEST or not isinstance(pk, str):
                    continue
                request_id = decode_uuid(pk.removeprefix("REQ#"), "PK")
                self.issue_keys[request_id] = issue_key
                request_ids.append(request_id)
        attempt_lists = await asyncio.gather(
            *(
                self._table.query(
                    key_condition="PK = :pk AND begins_with(SK, :prefix)",
                    values={":pk": keys.request_pk(rid), ":prefix": keys.SK_ATTEMPT},
                )
                for rid in request_ids
            )
        )
        return [self.add_attempt(item) for items in attempt_lists for item in items]

    async def load(self, attempt_ids: Iterable[uuid.UUID]) -> None:
        """Batched reads of what is not known yet: pointers, attempts, requests, repositories."""
        wanted = [aid for aid in dict.fromkeys(attempt_ids) if aid not in self.attempts]
        request_of: dict[uuid.UUID, uuid.UUID] = {}
        missing_pointers: list[uuid.UUID] = []
        for aid in wanted:
            cached = self._pointers.get(self._table.name, aid)
            if cached is None:
                missing_pointers.append(aid)
            else:
                request_of[aid] = cached
        if missing_pointers:
            found = await self._table.batch_get(keys.attempt_pointer(a) for a in missing_pointers)
            for aid in missing_pointers:
                pointer = found.get(keys.attempt_pointer(aid))
                if pointer is None:
                    self.attempts[aid] = None  # unknown attempt: no join data (LEFT JOIN)
                    continue
                rid = decode_uuid(pointer.get("request_id"), "request_id")
                self._pointers.put(self._table.name, aid, rid)
                request_of[aid] = rid
        attempt_keys = [keys.attempt(rid, aid) for aid, rid in request_of.items()]
        request_keys = [
            keys.request(rid)
            for rid in dict.fromkeys(request_of.values())
            if rid not in self.issue_keys
        ]
        found = await self._table.batch_get([*attempt_keys, *request_keys])
        for aid, rid in request_of.items():
            item = found.get(keys.attempt(rid, aid))
            if item is None:
                self.attempts[aid] = None
            else:
                self.add_attempt(item)
        for key in request_keys:
            item = found.get(key)
            if item is not None:
                self.add_request(item)
        repo_ids = {
            info.repository_id
            for info in self.attempts.values()
            if info is not None and info.repository_id not in self.repositories
        }
        repos = await self._table.batch_get(keys.repository(r) for r in sorted(repo_ids))
        for rid in repo_ids:
            repo = repos.get(keys.repository(rid))
            self.repositories[rid] = _str(repo, "repository_key") if repo is not None else None

    def row(self, record: LlmCallRecord) -> CostRow:
        info = self.attempts.get(record.attempt_id) if record.attempt_id is not None else None
        if info is None:
            return CostRow(record, None, None, None, None)
        return CostRow(
            record,
            self.issue_keys.get(info.request_id),
            info.number,
            info.status,
            self.repositories.get(info.repository_id),
        )


_POINTERS = AttemptPointers()


class DynamoUsageStore:
    def __init__(self, table: Table, *, pointers: AttemptPointers | None = None) -> None:
        self._table = table
        self._pointers = pointers if pointers is not None else _POINTERS

    # U1 --------------------------------------------------------------------------------------
    async def add(self, record: LlmCallRecord) -> bool:
        """True when stored; False when this call (id or provider message id) already exists."""
        call_id = str(record.call_id)
        created = encode_timestamp(record.created_at)
        guard = {"call_id": call_id, "created_at": created}
        duplicate = _AlreadyRecorded()
        tx = self._table.transaction()
        not_exists = "attribute_not_exists(PK)"
        tx.put(
            {**keys.llm_call_guard(call_id).as_dict(), "entity": "llm_call_guard", **guard},
            condition=not_exists,
            on_failed=duplicate,
        )
        if record.provider_message_id is not None:
            message_guard = keys.provider_message_guard(record.provider_message_id)
            tx.put(
                {**message_guard.as_dict(), "entity": "provider_message_guard", **guard},
                condition=not_exists,
                on_failed=duplicate,
            )
        if record.attempt_id is not None:
            tx.put(
                usage_item(record, keys.usage_attempt_pk(record.attempt_id)),
                condition=not_exists,
                on_failed=duplicate,
            )
        tx.put(
            usage_item(record, keys.usage_session_pk(str(record.session_id))),
            condition=not_exists,
            on_failed=duplicate,
        )
        try:
            await tx.commit()
        except _AlreadyRecorded:
            return False
        return True

    # U2-U5 -----------------------------------------------------------------------------------
    async def _ledger(self, pk: str) -> list[Item]:
        return await self._table.query(key_condition="PK = :pk", values={":pk": pk})

    async def rows(
        self,
        *,
        attempt_ids: Sequence[uuid.UUID] | None = None,
        issue_keys: Sequence[str] | None = None,
        session_id: uuid.UUID | None = None,
    ) -> list[CostRow]:
        if attempt_ids is None and issue_keys is None and session_id is None:
            raise ValueError("cost rows need an attempt, issue or session filter")
        if (attempt_ids is not None and not attempt_ids) or (
            issue_keys is not None and not issue_keys
        ):
            return []  # an empty IN list matches nothing
        joins = _Joins(self._table, self._pointers)
        partitions: list[str]
        if session_id is not None:  # U4
            partitions = [keys.usage_session_pk(str(session_id))]
        elif attempt_ids is not None:  # U2
            partitions = [keys.usage_attempt_pk(a) for a in dict.fromkeys(attempt_ids)]
        else:  # U3
            assert issue_keys is not None  # noqa: S101 - checked above
            story_attempts = await joins.issue_attempts(issue_keys)
            partitions = [keys.usage_attempt_pk(a) for a in story_attempts]
        pages = await asyncio.gather(*(self._ledger(pk) for pk in partitions))
        records = [_record(item) for page in pages for item in page]
        await joins.load(r.attempt_id for r in records if r.attempt_id is not None)
        result = [joins.row(r) for r in records]
        if attempt_ids is not None:
            wanted = set(attempt_ids)
            result = [r for r in result if r.record.attempt_id in wanted]
        if issue_keys is not None:
            issues = set(issue_keys)
            result = [r for r in result if r.issue_key in issues]
        if session_id is not None:
            result = [r for r in result if r.record.session_id == session_id]
        result.sort(key=_order)
        return result


def _order(row: CostRow) -> tuple[Any, uuid.UUID]:
    return (row.record.created_at, row.record.call_id)


__all__ = ["DynamoUsageStore", "usage_item"]
