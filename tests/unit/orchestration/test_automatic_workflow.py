"""The fully automated pipeline (WORKFLOW_APPROVAL_MODE=automatic): deterministic, hash-bound
policy decisions replace the two human gates; failures never publish and are reported once."""

from pathlib import Path
from typing import Any

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.artifacts.models import ArtifactKind
from ai_code_change_agent.application.dependencies.ports import DependencyBundle, LockedPackage
from ai_code_change_agent.domain.changes.models import ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.domain.dependencies.policy import DependencyRequest
from ai_code_change_agent.orchestration.steps import AUTOMATIC_ACTOR, CodeChangeSteps
from tests.support import workflow
from tests.support.changes import python_profile
from tests.support.workflow import make_rig

S = AttemptStatus


def plan_with(**over: Any) -> ImplementationPlan:
    return workflow.PLAN.model_copy(update=over)


async def run(rig: Any) -> AttemptStatus:
    await rig.runner.start(rig.attempt_id)
    return AttemptStatus((await rig.deps.changes.get_attempt(rig.attempt_id)).status)


async def kinds(rig: Any) -> list[ArtifactKind]:
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    return [m.kind for m in await rig.artifacts.list(CodeChangeSteps._scope(att))]


async def test_story_to_draft_pull_request_without_any_human_decision(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    assert await run(rig) is S.COMPLETED
    approvals = await rig.deps.changes.approvals(rig.attempt_id)
    assert [(a.gate, a.actor) for a in approvals] == [
        (ApprovalGate.PLAN, AUTOMATIC_ACTOR),
        (ApprovalGate.FINAL, AUTOMATIC_ACTOR),
    ]
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert {a.artifact_sha256 for a in approvals} == {att.plan_sha256, att.manifest_sha256}
    (published,) = rig.publisher.requests
    assert published.pull_request is not None and published.pull_request.draft
    ((key, text),) = rig.reporter.reports
    assert key == "SHOP-1" and "Draft PR:" in text and "Branch: aica/shop-1-a1-" in text
    assert "acme/shop-service" in text and "test PASSED" in text
    assert (await kinds(rig)).count(ArtifactKind.AUTOMATIC_DECISION) == 2


async def test_failed_validation_never_publishes_and_is_reported_once(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic", max_repairs=0)
    rig.agent.edits = [{"app/greeting.py": b"x = 1\n", ".github/workflows/ci.yml": b"x"}]
    assert await run(rig) is S.FAILED
    assert rig.publisher.requests == []
    ((_, text),) = rig.reporter.reports
    assert "validation_failed" in text and "Retryable: no" in text
    await CodeChangeSteps(rig.deps).cleanup(rig.attempt_id)  # a second cleanup: no second comment
    assert len(rig.reporter.reports) == 1


async def test_a_profile_without_a_test_command_uses_the_toolchain_default(
    tmp_path: Path,
) -> None:
    """Generic onboarding: no per-repository test command is needed; the python toolchain's
    standard command (python -m pytest -q) is applied at runtime and validation still runs."""
    profile = python_profile()
    profile["commands"] = [c for c in profile["commands"] if c["kind"] != "test"]
    profile["capabilities"] = ["lint_run"]
    rig = await make_rig(tmp_path, approval_mode="automatic", profile=profile)
    assert await run(rig) is S.COMPLETED
    assert len(rig.publisher.requests) == 1


async def test_an_invalid_dependency_request_is_rejected_before_implementation(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    workflow.FakeAgent.plan_result = plan_with(dependencies=["git+https://evil.invalid/x.git"])
    try:
        assert await run(rig) is S.REJECTED
    finally:
        workflow.FakeAgent.plan_result = None
    assert len(rig.sandboxes.started) == 1  # planning only
    assert "dependency_request_invalid" in rig.reporter.reports[0][1]


async def test_a_protected_path_in_the_plan_is_rejected(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    change = {"path": ".github/workflows/ci.yml", "operation": "modify", "purpose": "x"}
    workflow.FakeAgent.plan_result = ImplementationPlan.model_validate(
        {**workflow.PLAN.model_dump(), "changes": [change]}
    )
    try:
        assert await run(rig) is S.REJECTED
    finally:
        workflow.FakeAgent.plan_result = None
    assert "plan_touches_protected_path" in rig.reporter.reports[0][1]


class FakePreparer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[DependencyRequest, ...]] = []

    async def prepare(self, requests: tuple[DependencyRequest, ...]) -> DependencyBundle:
        self.calls.append(requests)
        site = self.root / "k1" / "site"
        site.mkdir(parents=True, exist_ok=True)
        pkg = LockedPackage("flask", "3.1.0", "flask-3.1.0-py3-none-any.whl", "a" * 64, 1)
        return DependencyBundle("k1", "b" * 64, str(site), (pkg,))


async def test_requested_dependencies_are_prepared_and_mounted_read_only(tmp_path: Path) -> None:
    preparer = FakePreparer(tmp_path / "deps")
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    workflow.FakeAgent.plan_result = plan_with(dependencies=["Flask==3.1.0"])
    try:
        assert await run(rig) is S.COMPLETED
    finally:
        workflow.FakeAgent.plan_result = None
    assert [r.requirement() for r in preparer.calls[0]] == ["flask==3.1.0"]
    assert len(preparer.calls) == 2  # implementation and validation use the same bundle
    implementation = rig.sandboxes.handles[1]
    assert implementation.dependency_dir == str((tmp_path / "deps" / "k1" / "site").resolve())
    validation = [c for c in rig.commands if len(c) > 1 and c[1] == "run"]
    assert all("dst=/opt/deps,readonly" in " ".join(c) for c in validation)
    assert ArtifactKind.DEPENDENCY_LOCK in await kinds(rig)


async def test_dependencies_without_a_preparer_fail_closed(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    workflow.FakeAgent.plan_result = plan_with(dependencies=["flask"])
    try:
        assert await run(rig) is S.FAILED
    finally:
        workflow.FakeAgent.plan_result = None
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == "dependency_preparation_unavailable"
    assert rig.publisher.requests == []


async def test_disabled_delivery_stops_before_publishing(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic", delivery=False)
    assert await run(rig) is S.AWAITING_FINAL_APPROVAL  # approved, waiting for delivery config
    assert rig.publisher.requests == [] and rig.reporter.reports == []


async def test_manual_mode_still_waits_for_an_operator(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    assert await run(rig) is S.AWAITING_PLAN_APPROVAL
    assert await rig.deps.changes.approvals(rig.attempt_id) == []
