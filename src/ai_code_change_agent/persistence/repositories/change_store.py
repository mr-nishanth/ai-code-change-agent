"""SQLAlchemy adapter for the change ports (one session = one unit of work)."""

import uuid
from types import TracebackType
from typing import Self

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.domain.changes.errors import ChangeConflictError
from ai_code_change_agent.domain.changes.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
    CodeChangeRequest,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.persistence.models.changes import (
    CodeChangeApprovalModel,
    CodeChangeAttemptModel,
)
from ai_code_change_agent.persistence.models.webhooks import CodeChangeRequest as RequestModel

_ATTEMPT_FIELDS = (
    "status",
    "base_commit_sha",
    "plan_ref",
    "plan_sha256",
    "manifest_ref",
    "manifest_sha256",
    "repair_count",
    "failure_code",
    "row_version",
)


def _request(m: RequestModel) -> CodeChangeRequest:
    return CodeChangeRequest(
        m.id,
        m.jira_issue_id,
        m.jira_issue_key,
        m.jira_project_key,
        m.repository_id,
        m.profile_version_id,
        m.current_attempt_id,
        m.status,
    )


def _attempt(m: CodeChangeAttemptModel) -> CodeChangeAttempt:
    return CodeChangeAttempt(
        id=m.id,
        request_id=m.request_id,
        number=m.number,
        repository_id=m.repository_id,
        profile_version_id=m.profile_version_id,
        status=AttemptStatus(m.status),
        base_commit_sha=m.base_commit_sha,
        plan_ref=m.plan_ref,
        plan_sha256=m.plan_sha256,
        manifest_ref=m.manifest_ref,
        manifest_sha256=m.manifest_sha256,
        repair_count=m.repair_count,
        failure_code=m.failure_code,
        row_version=m.row_version,
    )


def _approval(m: CodeChangeApprovalModel) -> Approval:
    return Approval(
        attempt_id=m.attempt_id,
        gate=ApprovalGate(m.gate),
        artifact_sha256=m.artifact_sha256,
        decision=ApprovalDecision(m.decision),
        actor=m.actor,
        actor_verified=m.actor_verified,
        id=m.id,
        decided_at=m.decided_at,
    )


class SqlChangeStore:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None:
        m = await self._s.get(RequestModel, request_id)
        return _request(m) if m else None

    async def lock_request(self, request_id: uuid.UUID) -> CodeChangeRequest | None:
        m = (
            await self._s.execute(
                select(RequestModel).where(RequestModel.id == request_id).with_for_update()
            )
        ).scalar_one_or_none()
        return _request(m) if m else None

    async def update_request(self, request: CodeChangeRequest) -> None:
        await self._s.execute(
            update(RequestModel)
            .where(RequestModel.id == request.id)
            .values(
                repository_id=request.repository_id,
                profile_version_id=request.profile_version_id,
                current_attempt_id=request.current_attempt_id,
                status=request.status,
            )
            .execution_options(synchronize_session=False)
        )

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None:
        m = await self._s.get(CodeChangeAttemptModel, attempt_id)
        return _attempt(m) if m else None

    async def lock_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt | None:
        m = (
            await self._s.execute(
                select(CodeChangeAttemptModel)
                .where(CodeChangeAttemptModel.id == attempt_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _attempt(m) if m else None

    async def list_attempts(self, request_id: uuid.UUID) -> list[CodeChangeAttempt]:
        rows = (
            await self._s.execute(
                select(CodeChangeAttemptModel)
                .where(CodeChangeAttemptModel.request_id == request_id)
                .order_by(CodeChangeAttemptModel.number)
            )
        ).scalars()
        return [_attempt(m) for m in rows]

    async def add_attempt(self, attempt: CodeChangeAttempt) -> None:
        self._s.add(
            CodeChangeAttemptModel(
                id=attempt.id,
                request_id=attempt.request_id,
                number=attempt.number,
                repository_id=attempt.repository_id,
                profile_version_id=attempt.profile_version_id,
                **{f: getattr(attempt, f) for f in _ATTEMPT_FIELDS if f != "status"},
                status=attempt.status.value,
            )
        )
        await self._s.flush()

    async def update_attempt(self, attempt: CodeChangeAttempt, *, expected_version: int) -> None:
        values = {f: getattr(attempt, f) for f in _ATTEMPT_FIELDS}
        values["status"] = attempt.status.value
        res = await self._s.execute(
            update(CodeChangeAttemptModel)
            .where(
                CodeChangeAttemptModel.id == attempt.id,
                CodeChangeAttemptModel.row_version == expected_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if getattr(res, "rowcount", 0) != 1:
            raise ChangeConflictError("attempt_changed_concurrently")

    async def add_approval(self, approval: Approval) -> None:
        self._s.add(
            CodeChangeApprovalModel(
                id=approval.id,
                attempt_id=approval.attempt_id,
                gate=approval.gate.value,
                artifact_sha256=approval.artifact_sha256,
                decision=approval.decision.value,
                actor=approval.actor,
                actor_verified=approval.actor_verified,
                decided_at=approval.decided_at,
            )
        )
        await self._s.flush()

    async def list_approvals(self, attempt_id: uuid.UUID) -> list[Approval]:
        rows = (
            await self._s.execute(
                select(CodeChangeApprovalModel)
                .where(CodeChangeApprovalModel.attempt_id == attempt_id)
                .order_by(CodeChangeApprovalModel.decided_at)
            )
        ).scalars()
        return [_approval(m) for m in rows]


class SqlChangeUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.store = SqlChangeStore(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None  # noqa: S101 - set in __aenter__
        await self._session.rollback()  # no-op after commit
        await self._session.close()

    async def commit(self) -> None:
        assert self._session is not None  # noqa: S101
        await self._session.commit()
