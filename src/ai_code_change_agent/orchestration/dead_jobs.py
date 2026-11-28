"""What a DEAD job leaves behind ends too (the worker's per-kind on_dead hooks).

A job ends DEAD when its last attempt fails or when its worker died on the last attempt (a claim
then finds the lease expired: `lease_expired_attempts_exhausted`). Either way the worker runs its
kind's hook (`Worker`, `JobQueue.dead_pending`), which ends the work the job owned through the
existing transitions, idempotently (every write is a compare-and-set; a duplicate run changes
nothing and reports nothing new):

* an attempt that is not terminal becomes FAILED with `job_attempts_exhausted` (its active guard is
  released with it), or COMPLETED through the report step when it was REPORTING (its pull request
  exists). One that died in DELIVERING is reported as delivered when its delivery result says the
  pull request was created (REPORTING, then the report step), else FAILED with
  `delivery_outcome_unknown`, whose comment says a branch or pull request may exist. Then it is
  reported once through the workflow's own once-per-attempt report
  (`CodeChangeSteps.cleanup`), or, for a multi-repository child, recorded by the coordinator
  (`child_finished`), which advances and finally reports its parent;
* a child that never got an attempt becomes FAILED and its parent advances;
* an intake job: see `IntakeHandler.on_dead` (the request ends BLOCKED, or its attempt as above).

A worker still running the job after its lease was lost cannot undo this: terminal attempts never
change again and its completion is refused (the job is DEAD)."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.jobs.queue import Job
from ai_code_change_agent.application.multirepo.coordinator import Coordinator
from ai_code_change_agent.application.multirepo.models import ChildStatus
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.changes.errors import InvalidTransitionError
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.orchestration.steps import DELIVERY_UNKNOWN

DEAD_JOB_CODE = "job_attempts_exhausted"
Launch = Callable[[uuid.UUID], Awaitable[Any]]
_log = get_logger("dead_jobs")


def _uuid(job: Job, field: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(job.payload[field]))
    except (KeyError, ValueError):
        return None


class AttemptRecovery:
    def __init__(
        self,
        *,
        changes: CodeChangeService,
        report: Launch,  # CodeChangeSteps.cleanup: reports a failed attempt once
        complete: Launch | None = None,  # CodeChangeSteps.report: REPORTING -> COMPLETED
        coordinator: Coordinator | None = None,
        delivered: Callable[[uuid.UUID], Awaitable[bool]] | None = None,  # PR created?
    ) -> None:
        self._delivered = delivered
        self._changes = changes
        self._report = report
        self._complete = complete
        self._coordinator = coordinator

    async def end_attempt(self, attempt_id: uuid.UUID, reason: str | None = None) -> None:
        """End a non-terminal attempt a DEAD job owned, then report or record it (idempotent)."""
        attempt = await self._changes.get_attempt(attempt_id)
        if not attempt.terminal:
            _log.warning("attempt_abandoned_by_dead_job", attempt_id=str(attempt_id),
                         status=attempt.status.value, reason=reason)  # fmt: skip
            code = DEAD_JOB_CODE
            if attempt.status is AttemptStatus.DELIVERING:
                if self._delivered is not None and await self._delivered(attempt_id):
                    with contextlib.suppress(InvalidTransitionError):
                        attempt = await self._changes.transition(
                            attempt_id, AttemptStatus.REPORTING
                        )
                else:  # it may have published before it died: never claim it did not
                    code = DELIVERY_UNKNOWN
            if attempt.status is AttemptStatus.REPORTING and self._complete is not None:
                await self._complete(attempt_id)  # the pull request exists: report it, COMPLETED
            elif not attempt.terminal:
                with contextlib.suppress(InvalidTransitionError):  # finished meanwhile
                    await self._changes.transition(
                        attempt_id, AttemptStatus.FAILED, failure_code=code
                    )
            attempt = await self._changes.get_attempt(attempt_id)
        if self._coordinator is not None:
            child = await self._coordinator.store.child_by_attempt(attempt_id)
            if child is not None:
                if attempt.terminal:
                    await self._coordinator.child_finished(child.id, attempt)
                return
            if await self._coordinator.store.find_by_request(attempt.request_id) is not None:
                return  # an old child attempt: only the parent's aggregated comment reports
        if attempt.terminal:
            await self._report(attempt_id)

    async def resume_dead(self, job: Job) -> None:
        """on_dead of `code_change.resume`: the attempt it was resuming ends."""
        attempt_id = _uuid(job, "attempt_id")
        if attempt_id is not None:
            await self.end_attempt(attempt_id, job.last_error)

    async def child_dead(self, job: Job) -> None:
        """on_dead of `code_change.child`: the child (and its attempt) ends; its parent advances."""
        child_id = _uuid(job, "child_id")
        if child_id is None or self._coordinator is None:
            return
        store = self._coordinator.store
        child = await store.child(child_id)
        if child is None:
            return
        if child.attempt_id is not None and not child.terminal:
            await self.end_attempt(child.attempt_id, job.last_error)
            return
        if not child.terminal:
            await store.update_child(
                child.id,
                frozenset({ChildStatus.QUEUED, ChildStatus.RUNNING}),
                status=ChildStatus.FAILED,
                failure_code=DEAD_JOB_CODE,
            )
        await self._coordinator.advance(child.parent_id)


__all__ = ["DEAD_JOB_CODE", "AttemptRecovery"]
