"""Persistence ports for code change requests, attempts and approvals."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from ai_code_change_agent.domain.changes.models import (
    Approval,
    CodeChangeAttempt,
    CodeChangeRequest,
)


@dataclass(frozen=True)
class AttemptListing:
    """An attempt with the times the store recorded (studio listings and headers)."""

    attempt: CodeChangeAttempt
    created_at: datetime
    updated_at: datetime


class ChangeStore(Protocol):
    async def get_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None: ...
    async def lock_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None: ...
    async def update_request(self, request: CodeChangeRequest) -> None: ...
    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None: ...
    async def lock_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None: ...
    async def list_attempts(self, request_id: uuid.UUID) -> list[CodeChangeAttempt]: ...
    async def add_attempt(self, attempt: CodeChangeAttempt) -> None: ...
    async def update_attempt(self, attempt: CodeChangeAttempt, *, expected_version: int) -> None:
        """Raises ChangeConflictError when the stored row_version differs (lost update)."""
        ...

    async def add_approval(self, approval: Approval) -> None: ...
    async def list_approvals(self, attempt_id: uuid.UUID) -> list[Approval]: ...

    # Studio (dev only, read-mostly) ------------------------------------------------------------
    async def list_recent_attempts(self, limit: int) -> list[AttemptListing]:
        """At most `limit` attempts, newest first (eventually consistent: may lag a write)."""
        ...

    async def get_attempt_listing(self, attempt_id: uuid.UUID) -> AttemptListing | None: ...

    async def record_agent_thread(self, attempt_id: uuid.UUID, thread_id: str) -> None:
        """Index the checkpoint thread of one agent run under its attempt; idempotent."""
        ...

    async def agent_threads(self, attempt_id: uuid.UUID) -> list[str]:
        """The indexed thread ids of the attempt, oldest run first."""
        ...


class ChangeUnitOfWork(Protocol):
    @property
    def store(self) -> ChangeStore: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
