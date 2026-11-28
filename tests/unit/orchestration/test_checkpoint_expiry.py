"""An approval that arrives after the attempt's LangGraph checkpoint expired (ACCA_CHECKPOINT_TTL_DAYS)
ends the attempt as FAILED with `checkpoint_expired` instead of a generic dead job."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from ai_code_change_agent.application.jobs.queue import Job, JobFailedError
from ai_code_change_agent.domain.changes.states import AttemptStatus, can_transition
from ai_code_change_agent.orchestration.intake import ResumeHandler
from ai_code_change_agent.orchestration.langgraph.runner import (
    CheckpointExpiredError,
    WorkflowRunner,
)
from tests.support.changes import MemoryChanges

S = AttemptStatus


class FakeGraph:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values
        self.invoked: list[Any] = []

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return SimpleNamespace(values=self.values, tasks=())

    async def ainvoke(self, command: Any, config: dict[str, Any]) -> dict[str, Any]:
        self.invoked.append(command)
        return {"status": "IMPLEMENTING"}


async def test_resume_without_a_checkpoint_raises_checkpoint_expired_and_runs_nothing() -> None:
    graph = FakeGraph({})
    with pytest.raises(CheckpointExpiredError) as raised:
        await WorkflowRunner(graph).resume(uuid.uuid4())
    assert raised.value.code == "checkpoint_expired" and graph.invoked == []


async def test_resume_with_a_checkpoint_resumes_the_thread() -> None:
    graph = FakeGraph({"attempt_id": "x"})
    assert await WorkflowRunner(graph).resume(uuid.uuid4()) == {"status": "IMPLEMENTING"}
    assert len(graph.invoked) == 1


def test_an_attempt_waiting_at_a_gate_can_fail() -> None:
    assert can_transition(S.AWAITING_PLAN_APPROVAL, S.FAILED)
    assert can_transition(S.AWAITING_FINAL_APPROVAL, S.FAILED)


@pytest.mark.parametrize("gate", [S.AWAITING_PLAN_APPROVAL, S.AWAITING_FINAL_APPROVAL])
async def test_an_expired_checkpoint_fails_the_attempt_and_releases_the_active_guard(
    gate: AttemptStatus,
) -> None:
    changes = MemoryChanges()
    request = changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    svc = changes.service()
    attempt = await svc.start_attempt(request.id)
    changes.force(attempt.id, status=gate)

    async def expired(attempt_id: uuid.UUID) -> None:
        raise CheckpointExpiredError

    handler = ResumeHandler(expired, changes=svc)
    job = Job(uuid.uuid4(), "code_change.resume", {"attempt_id": str(attempt.id)}, 1, 3)
    with pytest.raises(JobFailedError) as raised:
        await handler(job)
    assert raised.value.code == "checkpoint_expired"  # the job ends DEAD with this code
    failed = await svc.get_attempt(attempt.id)
    assert failed.status is S.FAILED and failed.failure_code == "checkpoint_expired"
    fresh = await svc.start_attempt(request.id)  # the guard no longer blocks a new attempt
    assert fresh.id != attempt.id and fresh.number == 2


async def test_an_expired_checkpoint_of_an_already_finished_attempt_changes_nothing() -> None:
    changes = MemoryChanges()
    request = changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    svc = changes.service()
    attempt = await svc.start_attempt(request.id)
    await svc.transition(attempt.id, S.CANCELLED)

    async def expired(attempt_id: uuid.UUID) -> None:
        raise CheckpointExpiredError

    job = Job(uuid.uuid4(), "code_change.resume", {"attempt_id": str(attempt.id)}, 1, 3)
    with pytest.raises(JobFailedError):
        await ResumeHandler(expired, changes=svc)(job)
    assert (await svc.get_attempt(attempt.id)).status is S.CANCELLED
