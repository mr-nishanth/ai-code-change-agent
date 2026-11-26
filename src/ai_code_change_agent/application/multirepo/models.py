"""Parent and child execution records (persisted in PostgreSQL)."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ParentStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ChildStatus(StrEnum):
    WAITING = "WAITING"  # its dependencies are not complete yet
    QUEUED = "QUEUED"  # a child job exists
    RUNNING = "RUNNING"  # its attempt runs
    COMPLETED = "COMPLETED"  # validated, draft PR created
    FAILED = "FAILED"
    REJECTED = "REJECTED"  # an automatic policy gate refused it
    BLOCKED = "BLOCKED"  # an upstream child failed or published no contract


CHILD_TERMINAL = frozenset(
    {ChildStatus.COMPLETED, ChildStatus.FAILED, ChildStatus.REJECTED, ChildStatus.BLOCKED}
)


@dataclass(frozen=True)
class Parent:
    id: uuid.UUID
    request_id: uuid.UUID
    status: ParentStatus
    declaration: dict[str, Any]
    report_round: int = 0
    jira_report: str | None = None


@dataclass(frozen=True)
class Child:
    id: uuid.UUID
    parent_id: uuid.UUID
    position: int
    repository_id: uuid.UUID
    profile_version_id: uuid.UUID
    repository_key: str
    reference: str
    role: str | None
    responsibility: str | None
    depends_on: tuple[str, ...]  # repository keys of sibling children
    status: ChildStatus
    runs: int = 0
    attempt_id: uuid.UUID | None = None
    contract_ref: str | None = None
    contract_sha256: str | None = None
    pr_url: str | None = None
    branch: str | None = None
    failure_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in CHILD_TERMINAL


class ContractDoc(BaseModel):
    """An upstream child's published interface (from its validated attempt); read-only context
    for the children that depend on it. Immutable: stored once, referenced by sha256."""

    model_config = ConfigDict(extra="forbid")

    parent_id: str
    repository_key: str
    role: str | None = None
    attempt_id: str
    implementation_report_sha256: str
    contract: str = Field(min_length=1, max_length=20_000)


class MultiRepoStore(Protocol):
    async def create(self, parent: Parent, children: list[Child]) -> tuple[Parent, list[Child]]:
        """Idempotent per request: an existing parent and its children are returned instead."""
        ...

    async def find_by_request(self, request_id: uuid.UUID) -> tuple[Parent, list[Child]] | None: ...
    async def load(self, parent_id: uuid.UUID) -> tuple[Parent, list[Child]]: ...
    async def child(self, child_id: uuid.UUID) -> Child | None: ...
    async def child_by_attempt(self, attempt_id: uuid.UUID) -> Child | None: ...
    async def update_child(
        self, child_id: uuid.UUID, expected: frozenset[ChildStatus], **fields: Any
    ) -> bool:
        """Compare-and-set on the status: False when the child was not in an expected status."""
        ...

    async def update_parent(
        self, parent_id: uuid.UUID, expected: frozenset[ParentStatus], **fields: Any
    ) -> bool: ...
    async def claim_report(self, parent_id: uuid.UUID) -> bool:
        """True for exactly one caller per report round (the aggregated comment is posted once)."""
        ...

    async def finish_report(self, parent_id: uuid.UUID, posted: bool) -> None: ...
    async def set_request_status(self, request_id: uuid.UUID, status: str) -> None: ...
