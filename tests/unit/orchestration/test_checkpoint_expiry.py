"""An approval that arrives after the attempt's LangGraph checkpoint expired (ACCA_CHECKPOINT_TTL_DAYS)
ends the attempt as FAILED with `checkpoint_expired` instead of a generic dead job."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ai_code_change_agent.application.jobs.queue import Job, JobFailedError
from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus, can_transition
from ai_code_change_agent.orchestration.intake import ResumeHandler
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.runner import (
    CheckpointExpiredError,
    WorkflowRunner,
)
from ai_code_change_agent.orchestration.steps import CodeChangeSteps
from tests.support.changes import MemoryChanges
from tests.support.workflow import Rig, make_rig

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


# ---- S2117: the expired attempt is reported to Jira once, like any other failed attempt -----


async def paused_rig(tmp_path: Any) -> tuple[Rig, CodeChangeSteps]:
    rig = await make_rig(tmp_path)  # manual approval: the workflow stops at the plan gate
    await rig.runner.start(rig.attempt_id)
    assert (await rig.deps.changes.get_attempt(rig.attempt_id)).status is S.AWAITING_PLAN_APPROVAL
    assert rig.reporter.reports == []
    return rig, CodeChangeSteps(rig.deps)


def resume_job(attempt_id: uuid.UUID) -> Job:
    return Job(uuid.uuid4(), "code_change.resume", {"attempt_id": str(attempt_id)}, 1, 3)


async def test_an_expired_checkpoint_is_reported_to_jira_exactly_once(tmp_path: Any) -> None:
    rig, steps = await paused_rig(tmp_path)
    lost = WorkflowRunner(build_graph(steps, MemorySaver()))  # the checkpoint row expired (TTL)
    handler = ResumeHandler(lost.resume, changes=rig.deps.changes, report=steps.cleanup)
    job = resume_job(rig.attempt_id)
    with pytest.raises(JobFailedError) as raised:
        await handler(job)
    assert raised.value.code == "checkpoint_expired"
    failed = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert failed.status is S.FAILED and failed.failure_code == "checkpoint_expired"
    ((issue, text),) = rig.reporter.reports
    assert issue == "SHOP-1" and "Outcome: FAILED" in text and "Reason: checkpoint_expired" in text
    with pytest.raises(JobFailedError):  # the RESUME job redelivered: still one comment
        await handler(resume_job(rig.attempt_id))
    with pytest.raises(JobFailedError):
        await handler(job)
    assert len(rig.reporter.reports) == 1


async def test_a_successful_resume_posts_no_failure_report(tmp_path: Any) -> None:
    rig, steps = await paused_rig(tmp_path)
    handler = ResumeHandler(rig.runner.resume, changes=rig.deps.changes, report=steps.cleanup)
    for gate in (ApprovalGate.PLAN, ApprovalGate.FINAL):
        attempt = await rig.deps.changes.get_attempt(rig.attempt_id)
        digest = attempt.plan_sha256 if gate is ApprovalGate.PLAN else attempt.manifest_sha256
        assert digest is not None
        await rig.deps.changes.decide(attempt.id, gate, digest, ApprovalDecision.APPROVED, "cli:op")
        await handler(resume_job(rig.attempt_id))
    assert (await rig.deps.changes.get_attempt(rig.attempt_id)).status is S.COMPLETED
    ((_, text),) = rig.reporter.reports  # only the workflow's own pull-request comment
    assert "created a draft pull request" in text and "checkpoint_expired" not in text


async def test_the_cost_report_sees_the_expired_attempt_as_failed(tmp_path: Any) -> None:
    rig, steps = await paused_rig(tmp_path)
    seen: list[AttemptStatus] = []

    def around(launch: Any) -> Any:  # what CostReporter.around does: look after the run
        async def run(attempt_id: uuid.UUID) -> Any:
            try:
                return await launch(attempt_id)
            finally:
                seen.append((await rig.deps.changes.get_attempt(attempt_id)).status)

        return run

    lost = WorkflowRunner(build_graph(steps, MemorySaver()))
    handler = ResumeHandler(
        lost.resume, changes=rig.deps.changes, report=steps.cleanup, around=around
    )
    with pytest.raises(JobFailedError):
        await handler(resume_job(rig.attempt_id))
    assert seen == [S.FAILED]  # not "AWAITING_PLAN_APPROVAL (not finished)"
