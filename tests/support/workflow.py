"""A full workflow rig on test doubles: real steps, real graph, real artifact store, real
validation engine (scripted docker client), scripted coding agent, recording publisher."""

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from ai_code_change_agent.agents.coding.configuration import StoryContext
from ai_code_change_agent.agents.coding.schemas import ImplementationPlan, ImplementationReport
from ai_code_change_agent.application.artifacts.service import ArtifactService
from ai_code_change_agent.application.git_providers.baseline import (
    BaselineReaders,
    ProviderRepository,
    VerifiedBaseline,
)
from ai_code_change_agent.application.git_providers.delivery import (
    DeliveryOutcome,
    DeliveryPublishers,
    DeliveryReceipt,
    DeliveryRequest,
)
from ai_code_change_agent.application.jira.models import JiraIssue, JiraProject, JiraRichText
from ai_code_change_agent.application.repositories.profile_reader import ProfileReader
from ai_code_change_agent.application.workspaces.models import BaselineFile, WorkspaceHandle
from ai_code_change_agent.application.workspaces.registry_baseline import RegistryBaselineProvider
from ai_code_change_agent.execution import fs
from ai_code_change_agent.execution.artifacts import LocalFilesystemArtifactStore
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from ai_code_change_agent.orchestration.steps import CodeChangeSteps, StepDependencies
from ai_code_change_agent.validation.engine import ValidationEngine
from tests.support.changes import MemoryChanges, python_profile
from tests.support.registry_fixtures import new_repo
from tests.support.registry_memory import InMemoryRegistry
from tests.unit.execution.fakes import make_runtime

BASE = "c" * 40
FILES = {"app/greeting.py": b"def greet():\n    return 'Hello'\n", "README.md": b"# Shop\n"}
PLAN = ImplementationPlan.model_validate(
    {
        "summary": "Return Goodbye",
        "acceptance_criteria": ["greet returns Goodbye"],
        "changes": [{"path": "app/greeting.py", "operation": "modify", "purpose": "new greeting"}],
        "tests": ["tests/test_greeting.py"],
    }
)
GOOD = b"def greet():\n    return 'Goodbye'\n"


class StubReader:
    def __init__(self) -> None:
        self.pins = 0

    async def pin(self, repository: ProviderRepository) -> str:
        self.pins += 1
        return BASE

    async def read(self, repository: ProviderRepository, commit_sha: str) -> VerifiedBaseline:
        return VerifiedBaseline(
            commit_sha, tuple(BaselineFile(p, c) for p, c in sorted(FILES.items()))
        )


@dataclass
class FakeSandbox:
    work: Path
    writable: bool
    disk_limit_exceeded: bool = False
    id: str = "aica-sbx-00000000-000000000000"


class FakeSandboxes:
    def __init__(self) -> None:
        self.started: list[FakeSandbox] = []
        self.handles: list[WorkspaceHandle] = []
        self.stopped = 0

    async def start(
        self, handle: WorkspaceHandle, work: Path, *, writable: bool, toolchain: str = "python"
    ) -> FakeSandbox:
        sb = FakeSandbox(work, writable)
        self.started.append(sb)
        self.handles.append(handle)
        return sb

    async def stop(self, sandbox: Any) -> None:
        self.stopped += 1


class FakeAgent:
    """Implementation edits files through the host path, as the sandbox tools would."""

    plan_result: ImplementationPlan | None = None  # a test may override the plan

    def __init__(self) -> None:
        self.edits: list[dict[str, bytes]] = [{"app/greeting.py": GOOD}]
        self.feedback: list[str | None] = []
        self.threads: list[str] = []

    async def plan(
        self, story: StoryContext, sandbox: Any, *, thread_id: str, checkpointer: Any = None
    ) -> ImplementationPlan:
        assert not sandbox.writable and story.base_commit_sha == BASE
        self.threads.append(thread_id)
        return self.plan_result or PLAN

    async def implement(
        self,
        story: StoryContext,
        plan: ImplementationPlan,
        sandbox: Any,
        *,
        thread_id: str,
        feedback: str | None = None,
        checkpointer: Any = None,
    ) -> ImplementationReport:
        assert sandbox.writable
        self.threads.append(thread_id)
        self.feedback.append(feedback)
        edits = self.edits.pop(0) if len(self.edits) > 1 else self.edits[0]
        for path, data in edits.items():
            fs.write_file(
                sandbox.work, path, data, create=not fs.exists_regular(sandbox.work, path)
            )
        return ImplementationReport(
            outcome="implemented", summary="done", changed_files=sorted(edits)
        )


class FakeJira:
    async def get_issue(self, issue_ref: str) -> JiraIssue:
        return JiraIssue(
            id="10001",
            key="SHOP-1",
            summary="Say goodbye",
            description=JiraRichText(text="Acceptance criteria:\n- greet returns Goodbye"),
            project=JiraProject(key="SHOP"),
        )

    async def get_comments(self, issue_ref: str) -> Any:  # pragma: no cover - unused
        raise NotImplementedError


class RecordingPublisher:
    def __init__(self) -> None:
        self.requests: list[DeliveryRequest] = []

    async def publish(self, request: DeliveryRequest) -> DeliveryReceipt:
        self.requests.append(request)
        return DeliveryReceipt(
            DeliveryOutcome.PULL_REQUEST_CREATED,
            branch=request.branch,
            commit_sha="e" * 40,
            pr_url="https://github.com/acme/shop-service/pull/7",
            pr_number=7,
            created=True,
        )


class FakeReporter:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    async def report(self, issue_key: str, text: str) -> str:
        self.reports.append((issue_key, text))
        return "POSTED"


@dataclass
class Rig:
    changes: MemoryChanges
    runner: WorkflowRunner
    attempt_id: uuid.UUID
    agent: FakeAgent
    publisher: RecordingPublisher
    reporter: FakeReporter
    sandboxes: FakeSandboxes
    artifacts: ArtifactService
    deps: StepDependencies
    commands: list[list[str]] = field(default_factory=list)


async def make_rig(
    tmp_path: Path,
    *,
    profile: dict[str, Any] | None = None,
    delivery: bool = True,
    max_repairs: int = 1,
    approval_mode: str = "manual",
    dependencies: Any = None,
) -> Rig:
    registry = InMemoryRegistry()
    svc = registry.service()
    await svc.register_repository(
        new_repo("shop-service", pid="424242", owner="acme", name="shop-service"), actor="cli:t"
    )
    await svc.create_version("shop-service", profile or python_profile(), actor="cli:t")
    await svc.publish_version("shop-service", 1, actor="cli:t")
    await svc.map_project("SHOP", "shop-service", actor="cli:t")
    await svc.enable("shop-service", actor="cli:t")
    async with registry.uow() as uow:
        (repo,) = await uow.store.list_repositories()
        (version,) = await uow.store.list_profile_versions(repo.id)
    changes = MemoryChanges()
    request = changes.add_request(repository_id=repo.id, profile_version_id=version.id)
    attempt = await changes.service(max_repairs=max_repairs).start_attempt(request.id)
    runtime, runner = make_runtime(tmp_path, dependency_root=tmp_path / "deps")
    artifacts = ArtifactService(
        LocalFilesystemArtifactStore(tmp_path / "artifacts", max_artifact_bytes=5_000_000),
        max_artifact_bytes=5_000_000,
        max_attempt_bytes=50_000_000,
    )
    agent, publisher, reporter, sandboxes = (
        FakeAgent(),
        RecordingPublisher(),
        FakeReporter(),
        FakeSandboxes(),
    )
    deps = StepDependencies(
        changes=changes.service(max_repairs=max_repairs),
        registry_uow=registry.uow,
        profiles=ProfileReader(registry.uow),
        baselines=RegistryBaselineProvider(registry.uow, BaselineReaders({"github": StubReader()})),
        runtime=runtime,
        sandboxes=sandboxes,
        agent=agent,
        engine=ValidationEngine(runtime),
        artifacts=artifacts,
        jira=FakeJira(),
        publishers=DeliveryPublishers({"github": publisher}),
        reporter=reporter,
        delivery_enabled=delivery,
        pr_create_enabled=delivery,
        max_repairs=max_repairs,
        approval_mode=approval_mode,
        dependencies=dependencies,
    )
    graph_runner = WorkflowRunner(build_graph(CodeChangeSteps(deps), MemorySaver()))
    return Rig(
        changes,
        graph_runner,
        attempt.id,
        agent,
        publisher,
        reporter,
        sandboxes,
        artifacts,
        deps,
        runner.calls,
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
