"""The parent LangGraph workflow end to end on test doubles: two human gates, independent
validation, bounded repair, delivery and reporting."""

import uuid
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.ports import GateState, StepResult
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from ai_code_change_agent.orchestration.steps import dependency_preparation
from tests.support.changes import python_profile
from tests.support.registry_fixtures import nestjs_backend
from tests.support.workflow import make_rig

S = AttemptStatus
PY = frozenset({"python"})


async def approve(rig: Any, gate: ApprovalGate) -> None:
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    digest = att.plan_sha256 if gate is ApprovalGate.PLAN else att.manifest_sha256
    await rig.deps.changes.decide(att.id, gate, digest, ApprovalDecision.APPROVED, "cli:op")


async def status(rig: Any) -> AttemptStatus:
    return AttemptStatus((await rig.deps.changes.get_attempt(rig.attempt_id)).status)


async def test_story_to_draft_pull_request_with_two_human_gates(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path)
    await rig.runner.start(rig.attempt_id)
    assert await status(rig) is S.AWAITING_PLAN_APPROVAL
    assert await rig.runner.waiting_for(rig.attempt_id) == "PLAN"
    await rig.runner.resume(rig.attempt_id)  # woken without a decision: stays paused
    assert await rig.runner.waiting_for(rig.attempt_id) == "PLAN"
    await approve(rig, ApprovalGate.PLAN)
    await rig.runner.resume(rig.attempt_id)
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.status is S.AWAITING_FINAL_APPROVAL and att.manifest_sha256
    assert rig.publisher.requests == []  # nothing is published before the final approval
    await approve(rig, ApprovalGate.FINAL)
    await rig.runner.resume(rig.attempt_id)
    assert await status(rig) is S.COMPLETED
    (request,) = rig.publisher.requests
    assert [(c.path, c.op.value) for c in request.changes] == [("app/greeting.py", "modify")]
    assert request.base_commit_sha == "c" * 40 and request.pull_request is not None
    assert request.pull_request.draft and request.branch.startswith("aica/shop-1-a1-")
    assert rig.reporter.reports and "pull/7" in rig.reporter.reports[0][1]
    assert [s.writable for s in rig.sandboxes.started] == [False, True]
    assert rig.sandboxes.stopped == 2
    runs = [c for c in rig.commands if len(c) > 1 and c[1] == "run"]
    assert {c[-1] for c in runs} >= {"-q", "app"}  # test and lint ran in a fresh workspace


async def test_a_validation_failure_is_repaired_once_with_feedback(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path)
    rig.agent.edits = [
        {"app/greeting.py": b"x = 1\n", "app/extra.py": b"y = 2\n"},
        {"app/greeting.py": b"def greet():\n    return 'Goodbye'\n"},
    ]
    await rig.runner.start(rig.attempt_id)
    await approve(rig, ApprovalGate.PLAN)
    await rig.runner.resume(rig.attempt_id)
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.status is S.AWAITING_FINAL_APPROVAL and att.repair_count == 1
    assert rig.agent.feedback[0] is None and "plan_scope" in str(rig.agent.feedback[1])


async def test_validation_failure_without_budget_fails_the_attempt(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path, max_repairs=0)
    rig.agent.edits = [{"app/greeting.py": b"x = 1\n", ".github/workflows/ci.yml": b"x"}]
    await rig.runner.start(rig.attempt_id)
    await approve(rig, ApprovalGate.PLAN)
    await rig.runner.resume(rig.attempt_id)
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.status is S.FAILED and att.failure_code == "validation_failed"
    assert rig.publisher.requests == []


async def test_a_rejected_plan_ends_the_workflow(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path)
    await rig.runner.start(rig.attempt_id)
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.plan_sha256 is not None
    await rig.deps.changes.decide(
        att.id, ApprovalGate.PLAN, att.plan_sha256, ApprovalDecision.REJECTED, "cli:op"
    )
    await rig.runner.resume(rig.attempt_id)
    assert await status(rig) is S.REJECTED
    assert [s.writable for s in rig.sandboxes.started] == [False]  # never implemented


async def test_a_toolchain_without_an_execution_image_blocks_the_attempt(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path, profile=nestjs_backend() | {"repository_key": "shop-service"})
    await rig.runner.start(rig.attempt_id)
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.status is S.BLOCKED and att.failure_code == "no_execution_image_for_toolchain"
    assert rig.sandboxes.started == [] and att.base_commit_sha is None


def test_dependencies_that_need_network_are_reported_not_enabled() -> None:
    base = python_profile()
    assert dependency_preparation(RepositoryProfile.model_validate(base), PY) is None
    networked = dict(
        base,
        commands=[
            *base["commands"],
            {
                "id": "deps",
                "kind": "install",
                "toolchain": "python",
                "argv": ["pip", "install", "-r", "requirements.txt"],
                "network": "package_registry",
            },
        ],
    )
    assert dependency_preparation(RepositoryProfile.model_validate(networked), PY) == (
        "dependency_preparation_requires_network"
    )


async def test_disabled_delivery_keeps_the_approved_attempt_waiting(tmp_path: Any) -> None:
    rig = await make_rig(tmp_path, delivery=False)
    await rig.runner.start(rig.attempt_id)
    await approve(rig, ApprovalGate.PLAN)
    await rig.runner.resume(rig.attempt_id)
    await approve(rig, ApprovalGate.FINAL)
    await rig.runner.resume(rig.attempt_id)
    assert await status(rig) is S.AWAITING_FINAL_APPROVAL and rig.publisher.requests == []


class ScriptedSteps:
    def __init__(self, outcomes: dict[str, list[StepResult]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.gates: dict[str, GateState] = {"PLAN": GateState.APPROVED, "FINAL": GateState.APPROVED}

    def _next(self, name: str) -> StepResult:
        self.calls.append(name)
        return self.outcomes[name].pop(0)

    async def prepare(self, attempt_id: uuid.UUID) -> StepResult:
        return self._next("prepare")

    async def plan(self, attempt_id: uuid.UUID) -> StepResult:
        return self._next("plan")

    async def gate(self, attempt_id: uuid.UUID, gate: str) -> GateState:
        self.calls.append(f"gate:{gate}")
        return self.gates[gate]

    async def implement(self, attempt_id: uuid.UUID, feedback: str | None) -> StepResult:
        return self._next("implement")

    async def validate(self, attempt_id: uuid.UUID) -> StepResult:
        return self._next("validate")

    async def deliver(self, attempt_id: uuid.UUID) -> StepResult:
        return self._next("deliver")

    async def report(self, attempt_id: uuid.UUID) -> StepResult:
        return self._next("report")

    async def cleanup(self, attempt_id: uuid.UUID) -> None:
        self.calls.append("cleanup")


def happy() -> dict[str, list[StepResult]]:
    return {
        "prepare": [StepResult("PLANNING")],
        "plan": [StepResult("AWAITING_PLAN_APPROVAL")],
        "implement": [StepResult("VALIDATING")],
        "validate": [StepResult("AWAITING_FINAL_APPROVAL")],
        "deliver": [StepResult("REPORTING")],
        "report": [StepResult("COMPLETED")],
    }


async def test_the_graph_routes_on_persisted_status_only() -> None:
    steps = ScriptedSteps(happy())
    out = await WorkflowRunner(build_graph(steps, MemorySaver())).start(uuid.uuid4())
    assert steps.calls == [
        "prepare",
        "plan",
        "gate:PLAN",
        "implement",
        "validate",
        "gate:FINAL",
        "deliver",
        "report",
        "cleanup",
    ]
    assert out["status"] == "COMPLETED"


@pytest.mark.parametrize("stop_at", ["prepare", "plan", "implement", "validate", "deliver"])
async def test_any_other_outcome_goes_to_cleanup(stop_at: str) -> None:
    outcomes = happy()
    outcomes[stop_at] = [StepResult("FAILED", failure_code="x")]
    steps = ScriptedSteps(outcomes)
    out = await WorkflowRunner(build_graph(steps, MemorySaver())).start(uuid.uuid4())
    assert steps.calls[-1] == "cleanup" and out["failure_code"] == "x"
    assert stop_at in steps.calls and "report" not in steps.calls


async def test_a_paused_workflow_resumes_in_a_new_process_from_the_checkpoint() -> None:
    saver = MemorySaver()
    steps = ScriptedSteps(happy())
    steps.gates["PLAN"] = GateState.PENDING
    attempt = uuid.uuid4()
    await WorkflowRunner(build_graph(steps, saver)).start(attempt)
    assert steps.calls == ["prepare", "plan", "gate:PLAN"]
    steps.gates["PLAN"] = GateState.APPROVED
    restarted = WorkflowRunner(build_graph(steps, saver))  # e.g. after a worker restart
    await restarted.start(attempt)  # start on an existing thread resumes, never re-runs
    assert steps.calls.count("prepare") == 1 and steps.calls[-1] == "cleanup"


async def test_validation_can_route_back_to_implementation() -> None:
    outcomes = happy()
    outcomes["implement"] = [StepResult("VALIDATING"), StepResult("VALIDATING")]
    outcomes["validate"] = [
        StepResult("IMPLEMENTING", feedback="fix"),
        StepResult("AWAITING_FINAL_APPROVAL"),
    ]
    steps = ScriptedSteps(outcomes)
    await WorkflowRunner(build_graph(steps, MemorySaver())).start(uuid.uuid4())
    assert steps.calls.count("implement") == 2 and steps.calls[-1] == "cleanup"
