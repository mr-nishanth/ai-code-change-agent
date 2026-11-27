"""DynamoDB store for multi-repository parents and children (DYNAMODB-ACCESS-PATTERNS.md 3, M1-M11).

Items (DYNAMODB-DATA-MODEL.md 4; the parent and its children live in the Story's item collection
`PK = REQ#<rid>`):
- `REQ#<rid>/PARENT`: one parent per request by key; `declaration_json`, the Jira report lifecycle
  (`report_round`, `jira_report` absent = PENDING, `jira_report_at`, attempts, comment id, error)
  and `row_version`.
- `REQ#<rid>/CHILD#<cid>`: a child; `depends_on_json` (an ordered JSON list), status, runs,
  `attempt_id` and delivery fields, `row_version`.
- `PARENT#<pid>/PTR` and `CHILD#<cid>/PTR`: immutable pointers to the request id (cached per store).
- `CHILDATT#<aid>/PTR` -> `(request_id, child_id)`: written, replaced or deleted in the same
  transaction that changes a child's `attempt_id`, and verified against `child.attempt_id` on read
  (M6), so an attempt is a child the moment it is linked and stops being one when a retry clears it.

`create` is one `TransactWriteItems` of at most 2 + 2 * 20 = 42 items, every one
`attribute_not_exists(PK)`; an existing parent makes it return what is stored (idempotent per
request). Child and parent updates are compare-and-set on the status: `False` when the stored
status is not expected. Time comes from the injected clock: `claim_report` treats an `IN_PROGRESS`
claim as stale once `jira_report_at` is `stale_seconds` older than `clock.now()`. Nothing is ever
deleted except a superseded `CHILDATT#` link. Failures are `PersistenceError` without driver text
(`table`)."""

import contextlib
import uuid
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    Parent,
    ParentStatus,
    ReportClaim,
    ReportState,
)
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.change_store import AttemptPointers
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_enum,
    decode_int,
    decode_json,
    decode_timestamp,
    decode_uuid,
    encode_json,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import (
    ConcurrentModificationError,
    CorruptItemError,
    Item,
    PersistenceError,
)
from ai_code_change_agent.persistence.dynamodb.table import Table
from ai_code_change_agent.persistence.dynamodb.transactions import VERSION

_NEW = "attribute_not_exists(PK)"
_CHILD_FIELDS = frozenset(
    {"status", "runs", "attempt_id", "contract_ref", "contract_sha256", "pr_url", "branch",
     "failure_code"}
)  # fmt: skip
_PARENT_FIELDS = frozenset({"status", "finished", "reopen"})
_UPDATE_ATTEMPTS = 5  # re-reads when the child changed between read and write (status still ok)


class _Refused(Exception):  # noqa: N818 - internal signal, not an error
    """A compare-and-set condition did not hold."""


def _str(item: Item, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        raise CorruptItemError(name)
    return value


def _opt_str(item: Item, name: str) -> str | None:
    return _str(item, name) if name in item else None


def _parent(item: Item) -> Parent:
    declaration = decode_json(item.get("declaration_json"), "declaration_json")
    if not isinstance(declaration, dict):
        raise CorruptItemError("declaration_json")
    report = _opt_str(item, "jira_report")
    if report is not None:
        decode_enum(ReportState, report, "jira_report")
    return Parent(
        id=decode_uuid(item.get("id"), "id"),
        request_id=decode_uuid(item.get("request_id"), "request_id"),
        status=decode_enum(ParentStatus, item.get("status"), "status"),
        declaration=declaration,
        report_round=decode_int(item.get("report_round"), "report_round"),
        jira_report=report,
        jira_comment_id=_opt_str(item, "jira_comment_id"),
        jira_report_attempts=decode_int(item.get("jira_report_attempts"), "jira_report_attempts"),
        jira_report_error=_opt_str(item, "jira_report_error"),
        completed_at=(
            decode_timestamp(item["completed_at"], "completed_at")
            if "completed_at" in item
            else None
        ),
    )


def _child(item: Item) -> Child:
    depends_on = decode_json(item.get("depends_on_json"), "depends_on_json")
    if not isinstance(depends_on, list) or not all(isinstance(k, str) for k in depends_on):
        raise CorruptItemError("depends_on_json")
    return Child(
        id=decode_uuid(item.get("id"), "id"),
        parent_id=decode_uuid(item.get("parent_id"), "parent_id"),
        position=decode_int(item.get("position"), "position"),
        repository_id=decode_uuid(item.get("repository_id"), "repository_id"),
        profile_version_id=decode_uuid(item.get("profile_version_id"), "profile_version_id"),
        repository_key=_str(item, "repository_key"),
        reference=_str(item, "reference"),
        role=_opt_str(item, "role"),
        responsibility=_opt_str(item, "responsibility"),
        depends_on=tuple(depends_on),
        status=decode_enum(ChildStatus, item.get("status"), "status"),
        runs=decode_int(item.get("runs"), "runs"),
        attempt_id=(
            decode_uuid(item["attempt_id"], "attempt_id") if "attempt_id" in item else None
        ),
        contract_ref=_opt_str(item, "contract_ref"),
        contract_sha256=_opt_str(item, "contract_sha256"),
        pr_url=_opt_str(item, "pr_url"),
        branch=_opt_str(item, "branch"),
        failure_code=_opt_str(item, "failure_code"),
    )


def _in(name: str, members: Iterable[str], values: dict[str, Any]) -> str:
    """`#name IN (:name0, ...)`; adds the values."""
    placeholders = []
    for i, member in enumerate(sorted(members)):
        values[f":{name}{i}"] = member
        placeholders.append(f":{name}{i}")
    return f"#{name} IN ({', '.join(placeholders)})"


def _child_values(fields: dict[str, Any]) -> dict[str, Any]:
    """Validated attribute values; None means REMOVE."""
    unknown = set(fields) - _CHILD_FIELDS
    if unknown:
        raise ValueError(f"unknown child fields: {sorted(unknown)}")
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if name == "status":
            out[name] = ChildStatus(value).value
        elif name == "runs":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("runs must be a non-negative integer")
            out[name] = value
        elif name == "attempt_id":
            out[name] = None if value is None else str(uuid.UUID(str(value)))
        else:
            out[name] = None if value is None else str(value)
    return out


class DynamoMultiRepoStore:
    """Implements `MultiRepoStore` on the application table."""

    def __init__(self, table: Table, clock: Clock | None = None) -> None:
        self._table = table
        self._clock: Clock = clock or SystemClock()
        self._parents = AttemptPointers()  # parent id -> request id (immutable)
        self._children = AttemptPointers()  # child id -> request id (immutable)

    def _now(self) -> str:
        return encode_timestamp(self._clock.now())

    # M1 ------------------------------------------------------------------------------------
    async def create(self, parent: Parent, children: list[Child]) -> tuple[Parent, list[Child]]:
        if len({c.position for c in children}) != len(children) or len(
            {c.repository_id for c in children}
        ) != len(children):
            raise ValueError("children must have distinct positions and repositories")
        now = self._now()
        rid = str(parent.request_id)
        tx = self._table.transaction()
        tx.put(
            {
                **keys.parent(parent.request_id).as_dict(),
                "entity": "multi_repo_parent",
                "id": str(parent.id),
                "request_id": rid,
                "status": ParentStatus(parent.status).value,
                "declaration_json": encode_json(parent.declaration),
                "report_round": 0,
                "jira_report_attempts": 0,
                VERSION: 1,
                "created_at": now,
                "updated_at": now,
            },
            condition=_NEW,
            on_failed=_Refused(),
        )
        tx.put(
            {
                **keys.parent_pointer(parent.id).as_dict(),
                "entity": "parent_pointer",
                "request_id": rid,
                "created_at": now,
            },
            condition=_NEW,
        )
        for c in children:
            item: Item = {
                **keys.child(parent.request_id, c.id).as_dict(),
                "entity": "multi_repo_child",
                "id": str(c.id),
                "parent_id": str(parent.id),
                "request_id": rid,
                "position": c.position,
                "repository_id": str(c.repository_id),
                "profile_version_id": str(c.profile_version_id),
                "repository_key": c.repository_key,
                "reference": c.reference,
                "depends_on_json": encode_json(list(c.depends_on)),
                "status": ChildStatus(c.status).value,
                "runs": 0,
                VERSION: 1,
                "created_at": now,
                "updated_at": now,
            }
            if c.role is not None:
                item["role"] = c.role
            if c.responsibility is not None:
                item["responsibility"] = c.responsibility
            tx.put(item, condition=_NEW)
            tx.put(
                {
                    **keys.child_pointer(c.id).as_dict(),
                    "entity": "child_pointer",
                    "request_id": rid,
                    "parent_id": str(parent.id),
                    "created_at": now,
                },
                condition=_NEW,
            )
        with contextlib.suppress(_Refused):  # the request has its parent: return what is stored
            await tx.commit()
        found = await self.find_by_request(parent.request_id)
        if found is None:  # pragma: no cover - the parent was just written or already existed
            raise PersistenceError("parent_not_found")
        return found

    # M2-M4 ---------------------------------------------------------------------------------
    async def find_by_request(self, request_id: uuid.UUID) -> tuple[Parent, list[Child]] | None:
        rows = await self._table.query(
            key_condition="PK = :pk AND SK BETWEEN :lo AND :hi",
            values={
                ":pk": keys.request_pk(request_id),
                ":lo": keys.SK_CHILD,
                ":hi": keys.SK_PARENT,
            },
        )
        parent = next((r for r in rows if r.get("SK") == keys.SK_PARENT), None)
        if parent is None:
            return None
        children = [_child(r) for r in rows if str(r.get("SK", "")).startswith(keys.SK_CHILD)]
        return _parent(parent), sorted(children, key=lambda c: c.position)

    async def _pointer(
        self, cache: AttemptPointers, key: keys.Key, entity_id: uuid.UUID
    ) -> uuid.UUID | None:
        cached = cache.get(self._table.name, entity_id)
        if cached is not None:
            return cached
        item = await self._table.get(key)
        if item is None:
            return None
        request_id = decode_uuid(item.get("request_id"), "request_id")
        cache.put(self._table.name, entity_id, request_id)
        return request_id

    async def _parent_request(self, parent_id: uuid.UUID) -> uuid.UUID | None:
        return await self._pointer(self._parents, keys.parent_pointer(parent_id), parent_id)

    async def load(self, parent_id: uuid.UUID) -> tuple[Parent, list[Child]]:
        request_id = await self._parent_request(parent_id)
        found = await self.find_by_request(request_id) if request_id is not None else None
        if found is None or found[0].id != parent_id:
            raise LookupError("parent_not_found")
        return found

    # M5-M6 ---------------------------------------------------------------------------------
    async def _child_item(self, child_id: uuid.UUID) -> Item | None:
        request_id = await self._pointer(self._children, keys.child_pointer(child_id), child_id)
        if request_id is None:
            return None
        return await self._table.get(keys.child(request_id, child_id))

    async def child(self, child_id: uuid.UUID) -> Child | None:
        item = await self._child_item(child_id)
        return _child(item) if item is not None else None

    async def child_by_attempt(self, attempt_id: uuid.UUID) -> Child | None:
        link = await self._table.get(keys.child_attempt_link(attempt_id))
        if link is None:
            return None
        request_id = decode_uuid(link.get("request_id"), "request_id")
        child_id = decode_uuid(link.get("child_id"), "child_id")
        item = await self._table.get(keys.child(request_id, child_id))
        if item is None:
            return None
        child = _child(item)
        return child if child.attempt_id == attempt_id else None  # a retry cleared it

    # M7 ------------------------------------------------------------------------------------
    async def update_child(
        self, child_id: uuid.UUID, expected: frozenset[ChildStatus], **fields: Any
    ) -> bool:
        values = _child_values(fields)
        allowed = {ChildStatus(s).value for s in expected}
        for _ in range(_UPDATE_ATTEMPTS):
            item = await self._child_item(child_id)
            if item is None or item.get("status") not in allowed:
                return False
            try:
                await self._write_child(item, allowed, values)
            except _Refused:
                continue  # changed since read: decide again on the fresh item
            return True
        raise ConcurrentModificationError()

    async def _write_child(self, item: Item, allowed: set[str], values: dict[str, Any]) -> None:
        names = {"#status": "status", "#row_version": VERSION}
        expr_values: dict[str, Any] = {
            ":seen": item[VERSION],
            ":one": 1,
            ":updated_at": self._now(),
        }
        condition = _in("status", allowed, expr_values) + " AND #row_version = :seen"
        sets, removes = ["updated_at = :updated_at"], []
        for name, value in values.items():
            names[f"#{name}"] = name
            if value is None:
                removes.append(f"#{name}")
            else:
                sets.append(f"#{name} = :v_{name}")
                expr_values[f":v_{name}"] = value
        update = "SET " + ", ".join(sets)
        if removes:
            update += " REMOVE " + ", ".join(removes)
        update += " ADD #row_version :one"
        key = keys.Key.of(item)
        old = item.get("attempt_id")
        new = values.get("attempt_id", old)
        if "attempt_id" not in values or new == old:
            await self._table.update(
                key,
                update=update,
                condition=condition,
                names=names,
                values=expr_values,
                on_failed=_Refused(),
            )
            return
        # The attempt link changes in the same transaction as the child (M6/M7).
        tx = self._table.transaction()
        tx.update(
            key,
            update=update,
            condition=condition,
            names=names,
            values=expr_values,
            on_failed=_Refused(),
        )
        mine = {":cid": item["id"]}
        if old is not None:
            tx.delete(
                keys.child_attempt_link(old),
                condition="attribute_not_exists(PK) OR child_id = :cid",
                values=mine,
                on_failed=ConcurrentModificationError(),
            )
        if new is not None:
            tx.put(
                {
                    **keys.child_attempt_link(new).as_dict(),
                    "entity": "child_attempt_link",
                    "request_id": item["request_id"],
                    "child_id": item["id"],
                    "created_at": expr_values[":updated_at"],
                },
                condition="attribute_not_exists(PK) OR child_id = :cid",
                values=mine,
                on_failed=PersistenceError("attempt_linked_to_another_child"),
            )
        await tx.commit()

    # M8 ------------------------------------------------------------------------------------
    async def update_parent(
        self, parent_id: uuid.UUID, expected: frozenset[ParentStatus], **fields: Any
    ) -> bool:
        unknown = set(fields) - _PARENT_FIELDS
        if unknown:
            raise ValueError(f"unknown parent fields: {sorted(unknown)}")
        request_id = await self._parent_request(parent_id)
        if request_id is None or not expected:
            return False
        now = self._now()
        names = {"#status": "status", "#row_version": VERSION}
        values: dict[str, Any] = {":one": 1, ":now": now}
        condition = "attribute_exists(PK) AND " + _in(
            "status", {ParentStatus(s).value for s in expected}, values
        )
        sets, removes, adds = ["updated_at = :now"], [], ["#row_version :one"]
        if "status" in fields:
            sets.append("#status = :new_status")
            values[":new_status"] = ParentStatus(fields["status"]).value
        if fields.get("reopen"):
            removes += ["completed_at", "jira_report", "jira_comment_id", "jira_report_error"]
            sets.append("jira_report_attempts = :zero")
            values[":zero"] = 0
            adds.append("report_round :one")
        elif fields.get("finished"):
            sets.append("completed_at = :now")
        update = "SET " + ", ".join(sets)
        if removes:
            update += " REMOVE " + ", ".join(removes)
        update += " ADD " + ", ".join(adds)
        try:
            await self._table.update(
                keys.parent(request_id),
                update=update,
                condition=condition,
                names=names,
                values=values,
                on_failed=_Refused(),
            )
        except _Refused:
            return False
        return True

    # M9-M10 --------------------------------------------------------------------------------
    async def claim_report(self, parent_id: uuid.UUID, stale_seconds: int) -> ReportClaim | None:
        request_id = await self._parent_request(parent_id)
        if request_id is None:
            return None
        now = self._clock.now()
        try:
            old = await self._table.update(
                keys.parent(request_id),
                update=(
                    "SET jira_report = :ip, jira_report_at = :now, updated_at = :now "
                    "ADD jira_report_attempts :one, #row_version :one"
                ),
                condition=(
                    "attribute_exists(PK) AND (attribute_not_exists(jira_report) "
                    "OR jira_report IN (:pending, :retryable) "
                    "OR (jira_report = :ip AND jira_report_at < :stale))"
                ),
                names={"#row_version": VERSION},
                values={
                    ":ip": ReportState.IN_PROGRESS.value,
                    ":pending": ReportState.PENDING.value,
                    ":retryable": ReportState.RETRYABLE_FAILURE.value,
                    ":now": encode_timestamp(now),
                    ":stale": encode_timestamp(now - timedelta(seconds=stale_seconds)),
                    ":one": 1,
                },
                on_failed=_Refused(),
                return_values="ALL_OLD",
            )
        except _Refused:
            return None
        previous = old.get("jira_report") or ReportState.PENDING.value
        return ReportClaim(
            decode_int(old.get("report_round"), "report_round"),
            decode_enum(ReportState, previous, "jira_report"),
            decode_int(old.get("jira_report_attempts"), "jira_report_attempts") + 1,
        )

    async def finish_report(
        self,
        parent_id: uuid.UUID,
        state: ReportState,
        *,
        comment_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Records the outcome of the claim in flight; a no-op when none is (as the SQL UPDATE)."""
        request_id = await self._parent_request(parent_id)
        if request_id is None:
            return
        values: dict[str, Any] = {
            ":state": ReportState(state).value,
            ":ip": ReportState.IN_PROGRESS.value,
            ":now": self._now(),
            ":one": 1,
        }
        sets = ["jira_report = :state", "jira_report_at = :now", "updated_at = :now"]
        if error is not None:
            sets.append("jira_report_error = :error")
            values[":error"] = error
        if comment_id is not None:
            sets.append("jira_comment_id = :comment")
            values[":comment"] = comment_id
        update = "SET " + ", ".join(sets)
        if error is None:
            update += " REMOVE jira_report_error"
        update += " ADD #row_version :one"
        try:
            await self._table.update(
                keys.parent(request_id),
                update=update,
                condition="jira_report = :ip",
                names={"#row_version": VERSION},
                values=values,
                on_failed=_Refused(),
            )
        except _Refused:
            return

    # M11 -----------------------------------------------------------------------------------
    async def set_request_status(self, request_id: uuid.UUID, status: str) -> None:
        """The request's status projection; bumps `row_version` so a change unit of work that read
        the request before this write cannot overwrite it. A missing request is a no-op."""
        try:
            await self._table.update(
                keys.request(request_id),
                update="SET #status = :status, updated_at = :now ADD #row_version :one",
                condition="attribute_exists(PK)",
                names={"#status": "status", "#row_version": VERSION},
                values={":status": status, ":now": self._now(), ":one": 1},
                on_failed=_Refused(),
            )
        except _Refused:
            return


__all__ = ["DynamoMultiRepoStore"]
