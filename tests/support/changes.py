"""In-memory change store and request fixtures (unit tests only)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from ai_code_change_agent.application.changes.ports import AttemptListing
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.domain.changes.errors import ChangeConflictError
from ai_code_change_agent.domain.changes.models import (
    Approval,
    CodeChangeAttempt,
    CodeChangeRequest,
)


class _State:
    def __init__(self) -> None:
        self.requests: dict[uuid.UUID, CodeChangeRequest] = {}
        self.attempts: dict[uuid.UUID, CodeChangeAttempt] = {}
        self.approvals: list[Approval] = []
        self.created: dict[uuid.UUID, datetime] = {}
        self.threads: dict[uuid.UUID, list[str]] = {}


class MemoryChangeStore:
    def __init__(self, state: _State) -> None:
        self.s = state

    async def get_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None:
        return self.s.requests.get(request_id)

    lock_request = get_request

    async def update_request(self, request: CodeChangeRequest) -> None:
        self.s.requests[request.id] = request

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None:
        return self.s.attempts.get(attempt_id)

    lock_attempt = get_attempt

    async def list_attempts(self, request_id: uuid.UUID) -> list[CodeChangeAttempt]:
        return sorted(
            (a for a in self.s.attempts.values() if a.request_id == request_id),
            key=lambda a: a.number,
        )

    async def add_attempt(self, attempt: CodeChangeAttempt) -> None:
        self.s.attempts[attempt.id] = attempt
        self.s.created[attempt.id] = datetime.now(UTC)

    async def update_attempt(self, attempt: CodeChangeAttempt, *, expected_version: int) -> None:
        if self.s.attempts[attempt.id].row_version != expected_version:
            raise ChangeConflictError("attempt_changed_concurrently")
        self.s.attempts[attempt.id] = attempt

    async def add_approval(self, approval: Approval) -> None:
        self.s.approvals.append(approval)

    async def list_approvals(self, attempt_id: uuid.UUID) -> list[Approval]:
        return [a for a in self.s.approvals if a.attempt_id == attempt_id]

    async def get_attempt_listing(self, attempt_id: uuid.UUID) -> AttemptListing | None:
        attempt = self.s.attempts.get(attempt_id)
        if attempt is None:
            return None
        created = self.s.created.get(attempt_id, datetime.now(UTC))
        return AttemptListing(attempt, created, created)

    async def list_recent_attempts(self, limit: int) -> list[AttemptListing]:
        listings = [await self.get_attempt_listing(a) for a in self.s.attempts]
        found = [x for x in listings if x is not None]
        return sorted(found, key=lambda x: x.created_at, reverse=True)[: max(limit, 0)]

    async def record_agent_thread(self, attempt_id: uuid.UUID, thread_id: str) -> None:
        runs = self.s.threads.setdefault(attempt_id, [])
        if thread_id not in runs:
            runs.append(thread_id)

    async def agent_threads(self, attempt_id: uuid.UUID) -> list[str]:
        return list(self.s.threads.get(attempt_id, []))


class MemoryChangeUow:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.store = MemoryChangeStore(state)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None


class MemoryChanges:
    def __init__(self) -> None:
        self.state = _State()

    def uow(self) -> MemoryChangeUow:
        return MemoryChangeUow(self.state)

    def service(self, **kw: Any) -> CodeChangeService:
        return CodeChangeService(self.uow, **kw)

    def add_request(
        self,
        *,
        repository_id: uuid.UUID | None = None,
        profile_version_id: uuid.UUID | None = None,
        key: str = "SHOP-1",
        project: str = "SHOP",
    ) -> CodeChangeRequest:
        req = CodeChangeRequest(
            uuid.uuid4(), "10001", key, project, repository_id, profile_version_id
        )
        self.state.requests[req.id] = req
        return req

    def force(self, attempt_id: uuid.UUID, **changes: Any) -> None:
        self.state.attempts[attempt_id] = replace(self.state.attempts[attempt_id], **changes)


def python_profile(key: str = "shop-service", **over: Any) -> dict[str, Any]:
    def cmd(cid: str, kind: str, *argv: str) -> dict[str, Any]:
        return {
            "id": cid,
            "kind": kind,
            "toolchain": "python",
            "argv": list(argv),
            "network": "none",
            "timeout_seconds": 120,
        }

    doc: dict[str, Any] = {
        "schema_version": 1,
        "repository_key": key,
        "languages": ["Python"],
        "toolchains": ["python"],
        "capabilities": ["test_run", "lint_run"],
        "command_policy": "default-v1",
        "validation_policy": "standard",
        "commands": [
            cmd("test", "test", "python", "-m", "pytest", "-q"),
            cmd("lint", "lint", "ruff", "check", "app"),
        ],
    }
    doc.update(over)
    return doc
