"""Parent and child execution records (persisted in PostgreSQL)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
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


class ReportState(StrEnum):
    """Lifecycle of the aggregated Jira comment of one report round. Separate from the
    parent's execution status: a reporting failure never changes what the code change did."""

    PENDING = "PENDING"  # (stored as NULL) not attempted yet in this round
    IN_PROGRESS = "IN_PROGRESS"  # claimed; a POST may be in flight (or the worker crashed)
    SUCCEEDED = "SUCCEEDED"  # Jira confirmed the comment; its id is stored
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"  # 429/5xx/timeout: reconcile, then retry
    PERMANENT_FAILURE = "PERMANENT_FAILURE"  # 401/403/404/invalid key or retries exhausted
    DISABLED = "DISABLED"  # JIRA_REPORTING_ENABLED=false


@dataclass(frozen=True)
class ReportClaim:
    round: int
    previous: ReportState  # IN_PROGRESS or RETRYABLE_FAILURE: the earlier POST may have landed
    attempts: int  # including this one


@dataclass(frozen=True)
class Parent:
    id: uuid.UUID
    request_id: uuid.UUID
    status: ParentStatus
    declaration: dict[str, Any]
    report_round: int = 0
    jira_report: str | None = None  # ReportState value; None = PENDING
    jira_comment_id: str | None = None
    jira_report_attempts: int = 0
    jira_report_error: str | None = None
    completed_at: datetime | None = None


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
    async def claim_report(self, parent_id: uuid.UUID, stale_seconds: int) -> ReportClaim | None:
        """Claim the current report round: PENDING or RETRYABLE_FAILURE, or IN_PROGRESS that
        has not moved for stale_seconds (a crashed worker). None for everyone else."""
        ...

    async def finish_report(
        self,
        parent_id: uuid.UUID,
        state: ReportState,
        *,
        comment_id: str | None = None,
        error: str | None = None,
    ) -> None: ...
    async def set_request_status(self, request_id: uuid.UUID, status: str) -> None: ...
