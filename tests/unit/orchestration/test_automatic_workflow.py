"""The fully automated pipeline (WORKFLOW_APPROVAL_MODE=automatic): deterministic, hash-bound
policy decisions replace the two human gates; failures never publish and are reported once."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.artifacts.models import ArtifactKind
from ai_code_change_agent.application.dependencies.ports import DependencyBundle, LockedPackage
from ai_code_change_agent.domain.changes.models import ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError, DependencyRequest
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.reporting import JiraOutcomeReporter
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


@pytest.mark.parametrize("code", [400, 401, 403])
@pytest.mark.parametrize("validation_passes", [True, False], ids=["completed", "failed"])
async def test_a_permanent_jira_comment_failure_is_not_retried_or_marked_reported(
    tmp_path: Path, code: int, validation_passes: bool
) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic", max_repairs=0)
    if not validation_passes:
        rig.agent.edits = [{"app/greeting.py": b"x = 1\n", ".github/workflows/ci.yml": b"x"}]
    posts: list[httpx.Request] = []

    def jira(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(code)

    rig.deps.reporter = JiraOutcomeReporter(  # the real reporter over a fake Jira
        enabled=True,
        base_url="https://x.atlassian.net",
        authenticator=BasicAuthenticator("a@b.c", SecretStr("tok")),
        transport=httpx.MockTransport(jira),
    )
    expected = S.COMPLETED if validation_passes else S.FAILED
    assert await run(rig) is expected  # reporting never changes the attempt outcome
    assert [(p.method, p.url.path) for p in posts] == [
        ("POST", "/rest/api/3/issue/SHOP-1/comment")
    ]  # one POST, not retried
    assert ArtifactKind.JIRA_REPORT not in await kinds(rig)  # not recorded as reported
    assert len(rig.publisher.requests) == int(validation_passes)


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
    def __init__(
        self, root: Path, locks: list[str] | None = None, error: str | None = None
    ) -> None:
        self.root = root
        self.calls: list[tuple[DependencyRequest, ...]] = []
        self.locks = locks or []  # lock hash per call (default: always the same)
        self.error = error

    async def prepare(self, requests: tuple[DependencyRequest, ...]) -> DependencyBundle:
        self.calls.append(requests)
        if self.error is not None:
            raise DependencyPolicyError(self.error)
        site = self.root / "k1" / "site"
        site.mkdir(parents=True, exist_ok=True)
        pkg = LockedPackage("flask", "3.1.0", "flask-3.1.0-py3-none-any.whl", "a" * 64, 1)
        lock = self.locks.pop(0) if self.locks else "b" * 64
        return DependencyBundle("k1", lock, str(site), (pkg,))


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


@pytest.mark.parametrize(
    "code",
    [
        "dependency_wheel_unavailable",
        "dependency_conflict",
        "dependency_bundle_corrupted",
        "dependency_preparation_timeout",
        "dependency_limit_exceeded",
        "dependency_image_unavailable",
    ],
)
async def test_a_dependency_failure_fails_the_attempt_without_a_pr(
    tmp_path: Path, code: str
) -> None:
    preparer = FakePreparer(tmp_path / "deps", error=code)
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    workflow.FakeAgent.plan_result = plan_with(dependencies=["flask"])
    try:
        assert await run(rig) is S.FAILED
    finally:
        workflow.FakeAgent.plan_result = None
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == code
    assert rig.publisher.requests == []
    assert not any(s.writable for s in rig.sandboxes.started)  # implementation never started


async def test_validation_refuses_a_bundle_that_changed_since_implementation(
    tmp_path: Path,
) -> None:
    preparer = FakePreparer(tmp_path / "deps", locks=["b" * 64, "c" * 64])
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    workflow.FakeAgent.plan_result = plan_with(dependencies=["flask"])
    try:
        assert await run(rig) is S.FAILED
    finally:
        workflow.FakeAgent.plan_result = None
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == "dependency_bundle_changed"
    assert rig.publisher.requests == []


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


def _patch_baseline_files(rig: Any, extra_files: dict[str, bytes]) -> None:
    from ai_code_change_agent.application.git_providers.baseline import VerifiedBaseline
    from ai_code_change_agent.application.workspaces.models import BaselineFile

    reader = rig.deps.baselines._readers.get("github")
    merged = {**workflow.FILES, **extra_files}

    async def custom_read(_repo: Any, commit_sha: str) -> VerifiedBaseline:
        return VerifiedBaseline(
            commit_sha, tuple(BaselineFile(p, c) for p, c in sorted(merged.items()))
        )

    reader.read = custom_read


async def test_repository_pyproject_runtime_and_dev_dependencies_are_provisioned(
    tmp_path: Path,
) -> None:
    preparer = FakePreparer(tmp_path / "deps")
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    _patch_baseline_files(
        rig,
        {
            "pyproject.toml": (
                b'[project]\nname = "shop-service"\nversion = "0.1.0"\n'
                b'requires-python = ">=3.12"\ndependencies = ["flask>=3.0"]\n'
                b'[project.optional-dependencies]\ndev = ["pytest>=8.3"]\n'
                b'docs = ["mkdocs>=1.6"]\n'
            )
        },
    )
    seen_story_deps: list[tuple[str, ...]] = []
    orig_plan = rig.agent.plan

    async def capture_plan(story: Any, sandbox: Any, **kw: Any) -> ImplementationPlan:
        seen_story_deps.append(story.repository_dependencies)
        return await orig_plan(story, sandbox, **kw)

    rig.agent.plan = capture_plan  # type: ignore[method-assign]
    assert await run(rig) is S.COMPLETED
    assert seen_story_deps == [("flask>=3.0", "pytest>=8.3")]
    assert [r.requirement() for r in preparer.calls[0]] == ["flask>=3.0", "pytest>=8.3"]
    assert len(preparer.calls) == 2
    attempt_kinds = await kinds(rig)
    assert ArtifactKind.REPOSITORY_DEPENDENCIES in attempt_kinds
    assert ArtifactKind.DEPENDENCY_LOCK in attempt_kinds


async def test_requirements_merge_compatibly_with_plan_dependencies(tmp_path: Path) -> None:
    preparer = FakePreparer(tmp_path / "deps")
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    _patch_baseline_files(
        rig,
        {
            "requirements.txt": b"flask>=3.0\n",
            "requirements-dev.txt": b"pytest>=8.3\n",
        },
    )
    workflow.FakeAgent.plan_result = plan_with(dependencies=["flask==3.1.0", "httpx>=0.28"])
    try:
        assert await run(rig) is S.COMPLETED
    finally:
        workflow.FakeAgent.plan_result = None
    assert [r.requirement() for r in preparer.calls[0]] == [
        "flask==3.1.0,>=3.0",
        "httpx>=0.28",
        "pytest>=8.3",
    ]


async def test_conflicting_plan_dependency_vs_manifest_is_rejected(tmp_path: Path) -> None:
    preparer = FakePreparer(tmp_path / "deps")
    rig = await make_rig(tmp_path, approval_mode="automatic", dependencies=preparer)
    _patch_baseline_files(rig, {"requirements.txt": b"flask==3.0.0\n"})
    workflow.FakeAgent.plan_result = plan_with(dependencies=["flask==3.1.0"])
    try:
        assert await run(rig) is S.REJECTED
    finally:
        workflow.FakeAgent.plan_result = None
    assert preparer.calls == []
    assert "dependency_conflict" in rig.reporter.reports[0][1]


@pytest.mark.parametrize(
    ("files", "expected_code"),
    [
        ({"requirements.txt": b"-e ../other\n"}, "dependency_manifest_invalid"),
        (
            {"pyproject.toml": b'[project]\nname = "x"\nrequires-python = ">=3.13"\n'},
            "dependency_python_unsupported",
        ),
        (
            {
                "pyproject.toml": (
                    b'[project]\nname = "x"\n'
                    b"[tool.uv]\nrequired-environments = [\"sys_platform == 'darwin'\"]\n"
                )
            },
            "dependency_platform_unsupported",
        ),
    ],
)
async def test_invalid_baseline_manifest_fails_fast_before_llm_plan(
    tmp_path: Path, files: dict[str, bytes], expected_code: str
) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    _patch_baseline_files(rig, files)
    called = 0

    async def fail_if_called(*_a: Any, **_k: Any) -> ImplementationPlan:
        nonlocal called
        called += 1
        return workflow.PLAN

    rig.agent.plan = fail_if_called  # type: ignore[method-assign]
    assert await run(rig) is S.FAILED
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == expected_code
    assert called == 0
    assert rig.publisher.requests == []
    assert expected_code in rig.reporter.reports[0][1]


async def test_discovered_manifest_without_preparer_fails_closed(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, approval_mode="automatic")
    _patch_baseline_files(rig, {"requirements.txt": b"flask==3.1.0\n"})
    assert await run(rig) is S.FAILED
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == "dependency_preparation_unavailable"
    assert rig.publisher.requests == []


# ----- the publishing boundary: no validation failure mode ever reaches the publisher -----------

_QUALITY_ARGV = {
    "test": ("python", "-m", "pytest", "-q"),
    "lint": ("ruff", "check", "app"),
    "format": ("ruff", "format", "--check", "app"),
    "types": ("mypy", "app"),
}
_QUALITY_KIND = {"test": "test", "lint": "lint", "format": "format_check", "types": "lint"}
_OK = ProcessResult(0, b"ok", b"", False, False)
_KEY = b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n"


def _quality_profile() -> dict[str, Any]:
    profile = python_profile(capabilities=["test_run", "lint_run", "format_check"])
    profile["commands"] = [
        {
            "id": cid,
            "kind": _QUALITY_KIND[cid],
            "toolchain": "python",
            "argv": list(argv),
            "network": "none",
            "timeout_seconds": 120,
        }
        for cid, argv in _QUALITY_ARGV.items()
    ]
    return profile


def _container(target: str, result: ProcessResult | BaseException) -> Any:
    argv = list(_QUALITY_ARGV[target])
    return lambda cli: result if cli[-len(argv) :] == argv else _OK


_EXIT_1 = ProcessResult(1, b"failed", b"", False, False)

Boundary = tuple[str | None, ProcessResult | BaseException | None, dict[str, bytes] | None, str]
BOUNDARY: dict[str, Boundary] = {
    "timeout": ("test", ProcessResult(None, b"", b"", False, True), None, "validation_failed"),
    "test_failure": ("test", _EXIT_1, None, "validation_failed"),
    "ruff_failure": ("lint", _EXIT_1, None, "validation_failed"),
    "format_failure": ("format", _EXIT_1, None, "validation_failed"),
    "mypy_failure": ("types", _EXIT_1, None, "validation_failed"),
    "lint_timeout": ("lint", ProcessResult(None, b"", b"", False, True), None, "validation_failed"),
    "executable_missing": (
        "test",
        ProcessResult(127, b"", b"not found", False, False),
        None,
        "validation_failed",
    ),
    "docker_crash": ("test", OSError("daemon gone"), None, "validation_failed"),
    "runtime_error": ("test", RuntimeError("runner bug"), None, "runtimeerror"),
    "secret_detected": (None, None, {"app/greeting.py": _KEY}, "validation_failed"),
    "protected_file_modified": (
        None,
        None,
        {"app/greeting.py": b"x = 1\n", ".github/workflows/ci.yml": b"x"},
        "validation_failed",
    ),
}


@pytest.mark.parametrize("case", list(BOUNDARY))
async def test_no_validation_failure_mode_reaches_the_publisher(tmp_path: Path, case: str) -> None:
    target, result, edits, code = BOUNDARY[case]
    rig = await make_rig(
        tmp_path, approval_mode="automatic", max_repairs=0, profile=_quality_profile()
    )
    if target is not None and result is not None:
        rig.deps.runtime._runner.on_run = _container(target, result)  # type: ignore[attr-defined]
    if edits is not None:
        rig.agent.edits = [edits]
    status = await run(rig)
    assert status is S.FAILED  # never COMPLETED
    att = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert att.failure_code == code
    assert rig.publisher.requests == []
    assert att.manifest_sha256 is None  # no delivery manifest, so nothing could be approved
    assert ArtifactKind.DELIVERY_MANIFEST not in await kinds(rig)
    assert [a.gate for a in await rig.deps.changes.approvals(rig.attempt_id)] == [ApprovalGate.PLAN]
    ((_, text),) = rig.reporter.reports
    assert code in text
