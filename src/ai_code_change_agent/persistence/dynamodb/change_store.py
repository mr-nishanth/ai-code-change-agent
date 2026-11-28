"""DynamoDB adapter for the change ports (DYNAMODB-ACCESS-PATTERNS.md 2, C1-C11).

One `DynamoChangeUnitOfWork` = one `TransactWriteItems` on commit (the `DynamoUnitOfWork` pattern in
`transactions.py`): reads are strongly consistent and remembered, writes are buffered
(read-your-writes), leaving the block without `commit()` discards everything. A lost optimistic race
is `ConcurrentModificationError`, which `CodeChangeService` retries as a whole method.

Items (DYNAMODB-DATA-MODEL.md 4, one item collection per Story, `PK = REQ#<rid>`):
- `REQ#<rid>/REQUEST`: the request and its status projection (`row_version`; GSI1 by issue key).
  Written by intake (`request_item`); this adapter only replaces it under its version.
- `REQ#<rid>/ATT#<aid>`: an attempt (`row_version`; GSI2 `LIST#ATTEMPT`). Every update conditions on
  the version seen, on the stored attempt not being terminal and on the base commit being unset or
  unchanged (this replaces the `attempt_guard` trigger).
- `ATTEMPT#<aid>/PTR`: immutable pointer to the request id, cached for the process lifetime (C4).
- `REQ#<rid>/ACTIVE#<repo>`: exists while a non-terminal attempt exists for that repository
  (replaces the partial unique index): created with the attempt, deleted in the transaction that
  makes it terminal.
- `REQ#<rid>/APR#<aid>#<decided_at>#<approval_id>`: append-only approvals, sorted by decision time.
- `ATTEMPT#<aid>/THREAD#<thread_id>`: one per agent run (L3), written when the run starts, so the
  studio finds an attempt's checkpoint threads (the checkpoint table has no thread-prefix query).

The studio's attempt list (S1) is GSI2 `LIST#ATTEMPT`, newest first (eventually consistent): the
index projects keys only, so the listed attempts are fetched with one eventually consistent
`BatchGetItem` (an attempt update then never writes GSI2).

Attempt numbers are allocated under the request's `row_version` (`add_attempt` bumps it), so two
attempts of one request never share a number. Nothing but an ACTIVE guard is ever deleted (C11)."""

import uuid
from datetime import datetime
from typing import Any, Self

from ai_code_change_agent.application.changes.ports import AttemptListing
from ai_code_change_agent.domain.changes.errors import (
    ActiveAttemptError,
    ChangeConflictError,
    ChangeNotFoundError,
    ConcurrentModificationError,
)
from ai_code_change_agent.domain.changes.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
    CodeChangeRequest,
)
from ai_code_change_agent.domain.changes.states import TERMINAL, AttemptStatus
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_enum,
    decode_int,
    decode_timestamp,
    decode_uuid,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import CorruptItemError, Failure, Item
from ai_code_change_agent.persistence.dynamodb.table import Table
from ai_code_change_agent.persistence.dynamodb.transactions import VERSION, DynamoUnitOfWork

GSI2 = "GSI2"
_TERMINAL_VALUES = tuple(sorted(status.value for status in TERMINAL))
_OPTIONAL_ATTEMPT_FIELDS = (
    "base_commit_sha",
    "plan_ref",
    "plan_sha256",
    "manifest_ref",
    "manifest_sha256",
    "failure_code",
)


def _str(item: Item, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        raise CorruptItemError(name)
    return value


def _opt_str(item: Item, name: str) -> str | None:
    return _str(item, name) if name in item else None


def _opt_uuid(item: Item, name: str) -> uuid.UUID | None:
    return decode_uuid(item[name], name) if name in item else None


def _set_optional(item: Item, name: str, value: object) -> None:
    """Absent attributes stand for None (conditions use `attribute_not_exists`)."""
    if value is None:
        item.pop(name, None)
    else:
        item[name] = str(value)


def _request(item: Item) -> CodeChangeRequest:
    return CodeChangeRequest(
        id=decode_uuid(item.get("id"), "id"),
        jira_issue_id=_str(item, "jira_issue_id"),
        jira_issue_key=_str(item, "jira_issue_key"),
        jira_project_key=_str(item, "jira_project_key"),
        repository_id=_opt_uuid(item, "repository_id"),
        profile_version_id=_opt_uuid(item, "profile_version_id"),
        current_attempt_id=_opt_uuid(item, "current_attempt_id"),
        status=_str(item, "status"),
    )


def _attempt(item: Item) -> CodeChangeAttempt:
    return CodeChangeAttempt(
        id=decode_uuid(item.get("id"), "id"),
        request_id=decode_uuid(item.get("request_id"), "request_id"),
        number=decode_int(item.get("number"), "number"),
        repository_id=decode_uuid(item.get("repository_id"), "repository_id"),
        profile_version_id=decode_uuid(item.get("profile_version_id"), "profile_version_id"),
        status=decode_enum(AttemptStatus, item.get("status"), "status"),
        base_commit_sha=_opt_str(item, "base_commit_sha"),
        plan_ref=_opt_str(item, "plan_ref"),
        plan_sha256=_opt_str(item, "plan_sha256"),
        manifest_ref=_opt_str(item, "manifest_ref"),
        manifest_sha256=_opt_str(item, "manifest_sha256"),
        repair_count=decode_int(item.get("repair_count"), "repair_count"),
        failure_code=_opt_str(item, "failure_code"),
        row_version=decode_int(item.get(VERSION), VERSION),
    )


def _listing(item: Item) -> AttemptListing:
    return AttemptListing(
        _attempt(item),
        decode_timestamp(item.get("created_at"), "created_at"),
        decode_timestamp(item.get("updated_at"), "updated_at"),
    )


def _approval(item: Item) -> Approval:
    verified = item.get("actor_verified")
    if not isinstance(verified, bool):
        raise CorruptItemError("actor_verified")
    return Approval(
        attempt_id=decode_uuid(item.get("attempt_id"), "attempt_id"),
        gate=decode_enum(ApprovalGate, item.get("gate"), "gate"),
        artifact_sha256=_str(item, "artifact_sha256"),
        decision=decode_enum(ApprovalDecision, item.get("decision"), "decision"),
        actor=_str(item, "actor"),
        actor_verified=verified,
        id=decode_uuid(item.get("id"), "id"),
        decided_at=decode_timestamp(item.get("decided_at"), "decided_at"),
    )


def request_item(request: CodeChangeRequest, *, created_at: datetime) -> Item:
    """The `REQ#<rid>/REQUEST` item as intake creates it (`row_version` 1)."""
    now = encode_timestamp(created_at)
    item: Item = {
        **keys.request(request.id).as_dict(),
        **keys.request_gsi1(request.jira_issue_key, created_at, request.id),
        "entity": "code_change_request",
        "id": str(request.id),
        "jira_issue_id": request.jira_issue_id,
        "jira_issue_key": request.jira_issue_key,
        "jira_project_key": request.jira_project_key,
        "status": request.status,
        VERSION: 1,
        "created_at": now,
        "updated_at": now,
    }
    _set_optional(item, "repository_id", request.repository_id)
    _set_optional(item, "profile_version_id", request.profile_version_id)
    _set_optional(item, "current_attempt_id", request.current_attempt_id)
    return item


def _write_attempt_fields(item: Item, attempt: CodeChangeAttempt) -> None:
    """The mutable attempt fields (identity, number, repository and profile never change)."""
    item["status"] = attempt.status.value
    item["repair_count"] = attempt.repair_count
    for name in _OPTIONAL_ATTEMPT_FIELDS:
        _set_optional(item, name, getattr(attempt, name))


def _attempt_refused(seen: int) -> Failure:
    """Why the guarded attempt update failed, from the item's old image."""

    def failure(old: Item | None) -> BaseException:
        if old is None or old.get(VERSION) != seen:
            return ConcurrentModificationError()
        if old.get("status") in _TERMINAL_VALUES:
            return ChangeConflictError("attempt_immutable")
        return ChangeConflictError("baseline_immutable")

    return failure


class AttemptPointers:
    """Process cache of the immutable `ATTEMPT#<aid>/PTR` -> request id (C4), bounded."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max = max_entries
        self._ids: dict[tuple[str, uuid.UUID], uuid.UUID] = {}

    def get(self, table: str, attempt_id: uuid.UUID) -> uuid.UUID | None:
        return self._ids.get((table, attempt_id))

    def put(self, table: str, attempt_id: uuid.UUID, request_id: uuid.UUID) -> None:
        if len(self._ids) >= self._max:
            self._ids.pop(next(iter(self._ids)))  # oldest first
        self._ids[(table, attempt_id)] = request_id


_POINTERS = AttemptPointers()


class DynamoChangeStore:
    def __init__(self, uow: "DynamoChangeUnitOfWork") -> None:
        self._u = uow

    def _now(self) -> str:
        return encode_timestamp(self._u.clock.now())

    # C1-C3 -------------------------------------------------------------------------------
    async def get_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None:
        item = await self._u.get(keys.request(request_id), depend=False)
        return _request(item) if item is not None else None

    async def lock_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None:
        # the commit carries row_version = :seen on REQUEST (written) or a ConditionCheck
        item = await self._u.get(keys.request(request_id))
        return _request(item) if item is not None else None

    async def update_request(self, request: CodeChangeRequest) -> None:
        item = await self._u.get(keys.request(request.id))
        if item is None:  # as an SQL UPDATE of a missing row
            return
        _set_optional(item, "repository_id", request.repository_id)
        _set_optional(item, "profile_version_id", request.profile_version_id)
        _set_optional(item, "current_attempt_id", request.current_attempt_id)
        item["status"] = request.status
        item["updated_at"] = self._now()
        self._u.replace(item)

    # C4-C6 -------------------------------------------------------------------------------
    async def _request_id(self, attempt_id: uuid.UUID) -> uuid.UUID | None:
        cached = self._u.pointers.get(self._u.table.name, attempt_id)
        if cached is not None:
            return cached
        key = keys.attempt_pointer(attempt_id)
        item = await self._u.get(key, depend=False)
        if item is None:
            return None
        request_id = decode_uuid(item.get("request_id"), "request_id")
        if not self._u.is_written(key):  # only committed pointers are cached
            self._u.pointers.put(self._u.table.name, attempt_id, request_id)
        return request_id

    async def _attempt_item(self, attempt_id: uuid.UUID, *, depend: bool) -> Item | None:
        request_id = await self._request_id(attempt_id)
        if request_id is None:
            return None
        return await self._u.get(keys.attempt(request_id, attempt_id), depend=depend)

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None:
        item = await self._attempt_item(attempt_id, depend=False)
        return _attempt(item) if item is not None else None

    async def lock_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None:
        item = await self._attempt_item(attempt_id, depend=True)
        return _attempt(item) if item is not None else None

    async def list_attempts(self, request_id: uuid.UUID) -> list[CodeChangeAttempt]:
        """C6: a consistent base-table query (a GSI would be eventually consistent)."""
        rows = await self._u.query(keys.request_pk(request_id), keys.SK_ATTEMPT)
        return sorted((_attempt(row) for row in rows), key=lambda a: a.number)

    # C7-C8 -------------------------------------------------------------------------------
    async def add_attempt(self, attempt: CodeChangeAttempt) -> None:
        request = await self._u.get(keys.request(attempt.request_id))
        if request is None:
            raise ChangeNotFoundError("request_not_found")
        now_dt = self._u.clock.now()
        now = encode_timestamp(now_dt)
        # First item: the number is allocated under the request version, so a lost race is
        # ConcurrentModificationError (retried) rather than a duplicate number.
        self._u.replace({**request, "updated_at": now})
        item: Item = {
            **keys.attempt(attempt.request_id, attempt.id).as_dict(),
            **keys.attempt_gsi2(now_dt, attempt.id),
            "entity": "attempt",
            "id": str(attempt.id),
            "request_id": str(attempt.request_id),
            "number": attempt.number,
            "repository_id": str(attempt.repository_id),
            "profile_version_id": str(attempt.profile_version_id),
            VERSION: attempt.row_version,
            "created_at": now,
            "updated_at": now,
        }
        _write_attempt_fields(item, attempt)
        self._u.create(item, on_exists=ConcurrentModificationError())
        self._u.create(
            {
                **keys.attempt_pointer(attempt.id).as_dict(),
                "entity": "attempt_pointer",
                "request_id": str(attempt.request_id),
                "number": attempt.number,
                "created_at": now,
            },
            on_exists=ConcurrentModificationError(),
        )
        if not attempt.terminal:
            self._u.create(
                {
                    **keys.active_attempt_guard(
                        attempt.request_id, attempt.repository_id
                    ).as_dict(),
                    "entity": "active_attempt",
                    "attempt_id": str(attempt.id),
                    "created_at": now,
                },
                on_exists=ActiveAttemptError(),
            )

    async def update_attempt(self, attempt: CodeChangeAttempt, *, expected_version: int) -> None:
        """Raises ChangeConflictError when the stored row_version differs (lost update), the stored
        attempt is terminal, or the base commit would change once set."""
        item = await self._attempt_item(attempt.id, depend=True)
        if item is None:
            raise ChangeConflictError("attempt_changed_concurrently")
        current = _attempt(item)
        if current.row_version != expected_version:
            raise ChangeConflictError("attempt_changed_concurrently")
        if current.terminal:
            raise ChangeConflictError("attempt_immutable")
        if (
            current.base_commit_sha is not None
            and attempt.base_commit_sha != current.base_commit_sha
        ):
            raise ChangeConflictError("baseline_immutable")
        _write_attempt_fields(item, attempt)
        item["updated_at"] = self._now()
        values: dict[str, Any] = {f":terminal{i}": v for i, v in enumerate(_TERMINAL_VALUES)}
        if attempt.base_commit_sha is None:
            baseline = "attribute_not_exists(base_commit_sha)"
        else:
            baseline = "attribute_not_exists(base_commit_sha) OR base_commit_sha = :base"
            values[":base"] = attempt.base_commit_sha
        terminal = ", ".join(f":terminal{i}" for i in range(len(_TERMINAL_VALUES)))
        self._u.replace(
            item,
            condition=f"NOT (#status IN ({terminal})) AND ({baseline})",
            names={"#status": "status"},
            values=values,
            on_failed=_attempt_refused(current.row_version),
        )
        if attempt.terminal:
            await self._release_guard(current)

    async def _release_guard(self, attempt: CodeChangeAttempt) -> None:
        key = keys.active_attempt_guard(attempt.request_id, attempt.repository_id)
        guard = await self._u.get(key, depend=False)
        if guard is None or guard.get("attempt_id") != str(attempt.id):
            return
        self._u.delete(key, condition="attempt_id = :aid", values={":aid": str(attempt.id)})

    # C9-C10 ------------------------------------------------------------------------------
    async def add_approval(self, approval: Approval) -> None:
        """Append-only. The commit also conditions on the attempt's version (a ConditionCheck for an
        approval, the attempt's own guarded update for a rejection)."""
        request_id = await self._request_id(approval.attempt_id)
        if request_id is None:
            raise ChangeNotFoundError("attempt_not_found")
        await self._u.get(keys.attempt(request_id, approval.attempt_id))
        key = keys.approval(request_id, approval.attempt_id, approval.decided_at, approval.id)
        self._u.create(
            {
                **key.as_dict(),
                "entity": "approval",
                "id": str(approval.id),
                "attempt_id": str(approval.attempt_id),
                "request_id": str(request_id),
                "gate": approval.gate.value,
                "artifact_sha256": approval.artifact_sha256,
                "decision": approval.decision.value,
                "actor": approval.actor,
                "actor_verified": approval.actor_verified,
                "decided_at": encode_timestamp(approval.decided_at),
            },
            on_exists=ChangeConflictError("approval_exists"),
        )

    async def list_approvals(self, attempt_id: uuid.UUID) -> list[Approval]:
        """C10: ascending by `decided_at` (the sort key), consistent."""
        request_id = await self._request_id(attempt_id)
        if request_id is None:
            return []
        rows = await self._u.query(keys.request_pk(request_id), keys.approval_prefix(attempt_id))
        return [_approval(row) for row in rows]

    # Studio: S1, S3, L3 ------------------------------------------------------------------
    async def list_recent_attempts(self, limit: int) -> list[AttemptListing]:
        """S1: GSI2 `LIST#ATTEMPT`, descending by creation time (eventually consistent)."""
        if limit < 1:
            return []
        index_rows = await self._u.table.query(
            index=GSI2,
            key_condition="GSI2PK = :pk",
            values={":pk": keys.GSI_LIST_ATTEMPT},
            forward=False,
            limit=limit,
            page_size=limit,
        )
        rows = await self._u.table.fetch(index_rows, consistent=False)
        return [_listing(row) for row in rows]

    async def get_attempt_listing(self, attempt_id: uuid.UUID) -> AttemptListing | None:
        item = await self._attempt_item(attempt_id, depend=False)
        return _listing(item) if item is not None else None

    async def record_agent_thread(self, attempt_id: uuid.UUID, thread_id: str) -> None:
        """L3: `ATTEMPT#<aid>/THREAD#<thread_id>`, created once (a second call changes nothing)."""
        if await self._request_id(attempt_id) is None:
            raise ChangeNotFoundError("attempt_not_found")
        key = keys.agent_thread(attempt_id, thread_id)
        if await self._u.get(key, depend=False) is not None:
            return
        self._u.create(
            {
                **key.as_dict(),
                "entity": "agent_thread",
                "attempt_id": str(attempt_id),
                "thread_id": thread_id,
                "started_at": self._now(),
            },
            on_exists=ConcurrentModificationError(),  # a racing writer: the retry sees it
        )

    async def agent_threads(self, attempt_id: uuid.UUID) -> list[str]:
        """L3: `begins_with(SK, 'THREAD#')` on `ATTEMPT#<aid>`, ordered by run start."""
        rows = await self._u.query(keys.attempt_pk(attempt_id), keys.SK_THREAD)
        runs = sorted((_str(row, "started_at"), _str(row, "thread_id")) for row in rows)
        return [thread_id for _, thread_id in runs]


class DynamoChangeUnitOfWork(DynamoUnitOfWork):
    """One transaction. Leaving the block without commit() discards everything."""

    def __init__(
        self,
        table: Table,
        clock: Clock | None = None,
        *,
        pointers: AttemptPointers | None = None,
    ) -> None:
        super().__init__(table)
        self.clock: Clock = clock or SystemClock()
        self.pointers = pointers if pointers is not None else _POINTERS
        self._store = DynamoChangeStore(self)

    @property
    def store(self) -> DynamoChangeStore:
        return self._store

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        return self


__all__ = [
    "AttemptPointers",
    "DynamoChangeStore",
    "DynamoChangeUnitOfWork",
    "request_item",
]
