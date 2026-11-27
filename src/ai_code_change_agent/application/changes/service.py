"""Code change use cases: the only way attempts change state or receive approvals.

Each method is one transaction: the attempt row is locked, the domain method decides, the request
status projection is updated in the same transaction. A mutation that loses an optimistic race
(`ConcurrentModificationError`) is retried as a whole method in a new unit of work (bounded, full
jitter; DYNAMODB-DATA-MODEL.md 5.1); genuine conflicts are not retried.
"""

import asyncio
import uuid
from collections.abc import Callable

from ai_code_change_agent.application.changes.ports import AttemptListing, ChangeUnitOfWork
from ai_code_change_agent.application.retry import Sleep, retry_concurrent
from ai_code_change_agent.domain.changes.errors import (
    ActiveAttemptError,
    ChangeNotFoundError,
    ImmutableAssignmentError,
)
from ai_code_change_agent.domain.changes.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
    CodeChangeRequest,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus

UowFactory = Callable[[], ChangeUnitOfWork]
S = AttemptStatus


class CodeChangeService:
    def __init__(
        self, uow: UowFactory, *, max_repairs: int = 1, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._uow = uow
        self._max_repairs = max_repairs
        self._sleep = sleep

    async def get_request(self, request_id: uuid.UUID) -> CodeChangeRequest:
        async with self._uow() as uow:
            req = await uow.store.get_request(request_id)
        if req is None:
            raise ChangeNotFoundError("request_not_found")
        return req

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt:
        async with self._uow() as uow:
            att = await uow.store.get_attempt(attempt_id)
        if att is None:
            raise ChangeNotFoundError("attempt_not_found")
        return att

    async def approvals(self, attempt_id: uuid.UUID) -> list[Approval]:
        async with self._uow() as uow:
            return await uow.store.list_approvals(attempt_id)

    async def recent_attempts(self, limit: int) -> list[AttemptListing]:
        """Newest first, at most `limit` (studio listing, eventually consistent)."""
        async with self._uow() as uow:
            return await uow.store.list_recent_attempts(limit)

    async def attempt_listing(self, attempt_id: uuid.UUID) -> AttemptListing:
        async with self._uow() as uow:
            listing = await uow.store.get_attempt_listing(attempt_id)
        if listing is None:
            raise ChangeNotFoundError("attempt_not_found")
        return listing

    @retry_concurrent
    async def record_agent_thread(self, attempt_id: uuid.UUID, thread_id: str) -> None:
        """Index an agent run's checkpoint thread (`attempt:<aid>:...`) under its attempt."""
        if not thread_id.startswith(f"attempt:{attempt_id}:"):
            raise ValueError("thread id does not belong to the attempt")
        async with self._uow() as uow:
            await uow.store.record_agent_thread(attempt_id, thread_id)
            await uow.commit()

    async def agent_threads(self, attempt_id: uuid.UUID) -> list[str]:
        async with self._uow() as uow:
            return await uow.store.agent_threads(attempt_id)

    @retry_concurrent
    async def assign_repository(
        self, request_id: uuid.UUID, repository_id: uuid.UUID, profile_version_id: uuid.UUID
    ) -> CodeChangeRequest:
        """Set from the resolver's result. Another repository is always refused; a different
        profile version of the same repository only when no attempt is active."""
        async with self._uow() as uow:
            req = await uow.store.lock_request(request_id)
            if req is None:
                raise ChangeNotFoundError("request_not_found")
            if req.repository_id is not None:
                if req.repository_id != repository_id:
                    raise ImmutableAssignmentError("repository_already_assigned")
                if req.profile_version_id == profile_version_id:
                    return req
                # A newer published profile of the same repository may replace a stale one, but
                # only between attempts: a running attempt keeps the profile it was pinned to.
                if any(not a.terminal for a in await uow.store.list_attempts(request_id)):
                    raise ImmutableAssignmentError("repository_already_assigned")
            req = CodeChangeRequest(
                req.id,
                req.jira_issue_id,
                req.jira_issue_key,
                req.jira_project_key,
                repository_id,
                profile_version_id,
                req.current_attempt_id,
                req.status,
            )
            await uow.store.update_request(req)
            await uow.commit()
            return req

    @retry_concurrent
    async def start_attempt(self, request_id: uuid.UUID) -> CodeChangeAttempt:
        """A new attempt, or the active one (idempotent: a re-delivered intake never adds one)."""
        async with self._uow() as uow:
            req = await uow.store.lock_request(request_id)
            if req is None:
                raise ChangeNotFoundError("request_not_found")
            if req.repository_id is None or req.profile_version_id is None:
                raise ActiveAttemptError("repository_not_assigned")
            attempts = await uow.store.list_attempts(request_id)
            active = [a for a in attempts if not a.terminal]
            if active:
                return active[0]
            attempt = CodeChangeAttempt(
                id=uuid.uuid4(),
                request_id=req.id,
                number=len(attempts) + 1,
                repository_id=req.repository_id,
                profile_version_id=req.profile_version_id,
            )
            await uow.store.add_attempt(attempt)
            await self._project(uow, req, attempt)
            await uow.commit()
            return attempt

    @retry_concurrent
    async def start_repository_attempt(
        self, request_id: uuid.UUID, repository_id: uuid.UUID, profile_version_id: uuid.UUID
    ) -> CodeChangeAttempt:
        """A multi-repository child's attempt: a new one, or the active one for this repository
        of the request (idempotent: a re-run job never adds a second)."""
        async with self._uow() as uow:
            req = await uow.store.lock_request(request_id)
            if req is None:
                raise ChangeNotFoundError("request_not_found")
            if req.repository_id is not None:
                raise ImmutableAssignmentError("single_repository_request")
            attempts = await uow.store.list_attempts(request_id)
            active = [a for a in attempts if not a.terminal and a.repository_id == repository_id]
            if active:
                return active[0]
            attempt = CodeChangeAttempt(
                id=uuid.uuid4(),
                request_id=req.id,
                number=max((a.number for a in attempts), default=0) + 1,
                repository_id=repository_id,
                profile_version_id=profile_version_id,
            )
            await uow.store.add_attempt(attempt)
            await uow.commit()
            return attempt

    @retry_concurrent
    async def _mutate(
        self,
        attempt_id: uuid.UUID,
        change: Callable[[CodeChangeAttempt, list[Approval]], CodeChangeAttempt],
    ) -> CodeChangeAttempt:
        async with self._uow() as uow:
            current = await uow.store.lock_attempt(attempt_id)
            if current is None:
                raise ChangeNotFoundError("attempt_not_found")
            approvals = await uow.store.list_approvals(attempt_id)
            updated = change(current, approvals)
            await uow.store.update_attempt(updated, expected_version=current.row_version)
            req = await uow.store.lock_request(updated.request_id)
            if req is not None:
                await self._project(uow, req, updated)
            await uow.commit()
            return updated

    @staticmethod
    async def _project(
        uow: ChangeUnitOfWork, req: CodeChangeRequest, att: CodeChangeAttempt
    ) -> None:
        if req.repository_id is None:  # multi-repository: the parent execution owns the status
            return
        await uow.store.update_request(
            CodeChangeRequest(
                req.id,
                req.jira_issue_id,
                req.jira_issue_key,
                req.jira_project_key,
                req.repository_id,
                req.profile_version_id,
                att.id,
                att.status.value,
            )
        )

    async def transition(
        self, attempt_id: uuid.UUID, target: AttemptStatus, *, failure_code: str | None = None
    ) -> CodeChangeAttempt:
        return await self._mutate(
            attempt_id,
            lambda a, ap: a.transition(
                target, ap, max_repairs=self._max_repairs, failure_code=failure_code
            ),
        )

    async def pin_baseline(self, attempt_id: uuid.UUID, commit_sha: str) -> CodeChangeAttempt:
        return await self._mutate(attempt_id, lambda a, _: a.pin_baseline(commit_sha))

    async def record_plan(self, attempt_id: uuid.UUID, ref: str, sha256: str) -> CodeChangeAttempt:
        return await self._mutate(attempt_id, lambda a, _: a.record_plan(ref, sha256))

    async def record_manifest(
        self, attempt_id: uuid.UUID, ref: str, sha256: str
    ) -> CodeChangeAttempt:
        return await self._mutate(attempt_id, lambda a, _: a.record_manifest(ref, sha256))

    @retry_concurrent
    async def decide(
        self,
        attempt_id: uuid.UUID,
        gate: ApprovalGate,
        artifact_sha256: str,
        decision: ApprovalDecision,
        actor: str,
    ) -> tuple[Approval, CodeChangeAttempt]:
        """Record an operator decision bound to the exact artifact hash. A rejection ends the
        attempt (REJECTED); an approval leaves the transition to the workflow, which re-checks
        the approval against the current artifact before acting on it."""
        async with self._uow() as uow:
            attempt = await uow.store.lock_attempt(attempt_id)
            if attempt is None:
                raise ChangeNotFoundError("attempt_not_found")
            approval = attempt.decide(gate, artifact_sha256, decision, actor)
            await uow.store.add_approval(approval)
            if decision is ApprovalDecision.REJECTED:
                rejected = attempt.transition(S.REJECTED)
                await uow.store.update_attempt(rejected, expected_version=attempt.row_version)
                req = await uow.store.lock_request(attempt.request_id)
                if req is not None:
                    await self._project(uow, req, rejected)
                attempt = rejected
            await uow.commit()
            return approval, attempt
