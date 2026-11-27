"""The workflow steps: application services the graph nodes call. Each step reloads its inputs
from the database and the artifact store, persists its outcome as an attempt transition, and is
safe to call again after a crash (it does nothing when the attempt is past its stage).

Workspaces never outlive a step: a sandbox is started for one phase and destroyed in `finally`.
"""

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from ai_code_change_agent.agents.coding.configuration import MultiRepoBrief, StoryContext
from ai_code_change_agent.agents.coding.schemas import ImplementationPlan, ImplementationReport
from ai_code_change_agent.application.artifacts.hashing import load_verified, store_model
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.artifacts.ports import ScopedArtifacts
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.delivery.manifest import DeliveryManifest
from ai_code_change_agent.application.delivery.naming import branch_name, commit_message
from ai_code_change_agent.application.delivery.pull_request import build_pull_request
from ai_code_change_agent.application.dependencies.ports import DependencyBundle, DependencyPreparer
from ai_code_change_agent.application.git_providers.baseline import VerifiedBaseline
from ai_code_change_agent.application.git_providers.delivery import (
    BaselineEntry,
    DeliveryOutcome,
    DeliveryPublishers,
    DeliveryRequest,
    DeliveryTarget,
)
from ai_code_change_agent.application.jira.ports import JiraIssueReader
from ai_code_change_agent.application.jira.story import story_of
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.application.repositories.profile_reader import ProfileReader
from ai_code_change_agent.application.workspaces.errors import WorkspaceLimitError
from ai_code_change_agent.application.workspaces.models import (
    TreeEntry,
    TreeManifest,
    WorkspaceHandle,
    WorkspaceSpec,
)
from ai_code_change_agent.application.workspaces.paths import is_protected, validate_relative_path
from ai_code_change_agent.application.workspaces.registry_baseline import RegistryBaselineProvider
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.changes.errors import InvalidTransitionError
from ai_code_change_agent.domain.changes.models import (
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
)
from ai_code_change_agent.domain.changes.states import TERMINAL, AttemptStatus
from ai_code_change_agent.domain.dependencies.manifests import (
    CategorizedDependencies,
    ManifestDiscoveryResult,
    discover_repository_dependencies,
    merge_dependency_requests,
)
from ai_code_change_agent.domain.dependencies.policy import (
    DependencyPolicyError,
    DependencyRequest,
)
from ai_code_change_agent.domain.repositories.command_policy import CommandKind
from ai_code_change_agent.domain.repositories.models import MappingStatus, RepositoryStatus
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.orchestration.langgraph.ports import (
    GateState,
    StepResult,
    checked_thread_id,
)
from ai_code_change_agent.validation.engine import ValidationEngine, ValidationInput
from ai_code_change_agent.validation.git_inspector import (
    changed_paths,
    changeset,
    expected_manifest,
)
from ai_code_change_agent.validation.schemas import ChangesetDocument, ValidationReport

S = AttemptStatus
_log = get_logger("workflow")


class SandboxStarter(Protocol):
    async def start(
        self, handle: WorkspaceHandle, work: Path, *, writable: bool, toolchain: str = "python"
    ) -> Any: ...
    async def stop(self, sandbox: Any) -> None: ...


class Runtime(Protocol):  # the parts of DockerWorkspaceRuntime the steps use
    async def create(self, spec: WorkspaceSpec, baseline: Any) -> WorkspaceHandle: ...
    async def manifest(self, handle: WorkspaceHandle, tree: Any = "work") -> TreeManifest: ...
    async def read_file(
        self, handle: WorkspaceHandle, path: str, *, tree: Any = "work", max_bytes: int = 262_144
    ) -> bytes: ...
    async def destroy(self, handle: WorkspaceHandle) -> Any: ...
    async def destroy_attempt_workspaces(self, attempt_id: uuid.UUID) -> int: ...
    def tree_path(self, handle: WorkspaceHandle, tree: Any = "work") -> Path: ...


class Agent(Protocol):
    async def plan(
        self, story: StoryContext, sandbox: Any, *, thread_id: str, checkpointer: Any = None
    ) -> ImplementationPlan: ...
    async def implement(
        self,
        story: StoryContext,
        plan: ImplementationPlan,
        sandbox: Any,
        *,
        thread_id: str,
        feedback: str | None = None,
        checkpointer: Any = None,
    ) -> ImplementationReport: ...


class Reporter(Protocol):
    async def report(self, issue_key: str, text: str) -> Any: ...


class DependencyLockDoc(BaseModel):
    key: str
    lock_sha256: str
    packages: list[dict[str, Any]]
    sources: list[str] = Field(default_factory=list)
    categories: dict[str, list[str]] = Field(default_factory=dict)


class SerializedDependencyRequest(BaseModel):
    name: str
    specifiers: list[str] = Field(default_factory=list)
    extras: list[str] = Field(default_factory=list)
    hashes: list[str] = Field(default_factory=list)

    def to_request(self) -> DependencyRequest:
        return DependencyRequest(
            name=self.name,
            specifiers=tuple(self.specifiers),
            extras=tuple(self.extras),
            hashes=tuple(self.hashes),
        )

    @classmethod
    def from_request(cls, req: DependencyRequest) -> "SerializedDependencyRequest":
        return cls(
            name=req.name,
            specifiers=list(req.specifiers),
            extras=list(req.extras),
            hashes=list(req.hashes),
        )


class RepositoryDependenciesDoc(BaseModel):
    source_kind: Literal["uv.lock", "pyproject.toml", "requirements.txt", "none"] = "none"
    sources: list[str] = Field(default_factory=list)
    requires_python: str | None = None
    runtime: list[SerializedDependencyRequest] = Field(default_factory=list)
    test_dev: list[SerializedDependencyRequest] = Field(default_factory=list)
    optional: dict[str, list[SerializedDependencyRequest]] = Field(default_factory=dict)
    build_system: list[str] = Field(default_factory=list)
    provisioned: list[SerializedDependencyRequest] = Field(default_factory=list)

    @classmethod
    def from_result(cls, res: ManifestDiscoveryResult) -> "RepositoryDependenciesDoc":
        return cls(
            source_kind=res.source_kind,
            sources=list(res.sources),
            requires_python=res.requires_python,
            runtime=[SerializedDependencyRequest.from_request(r) for r in res.categories.runtime],
            test_dev=[SerializedDependencyRequest.from_request(r) for r in res.categories.test_dev],
            optional={
                k: [SerializedDependencyRequest.from_request(r) for r in v]
                for k, v in res.categories.optional.items()
            },
            build_system=list(res.build_system),
            provisioned=[SerializedDependencyRequest.from_request(r) for r in res.provisioned],
        )

    def to_result(self) -> ManifestDiscoveryResult:
        return ManifestDiscoveryResult(
            source_kind=self.source_kind,
            sources=tuple(self.sources),
            requires_python=self.requires_python,
            categories=CategorizedDependencies(
                runtime=tuple(r.to_request() for r in self.runtime),
                test_dev=tuple(r.to_request() for r in self.test_dev),
                optional={k: tuple(r.to_request() for r in v) for k, v in self.optional.items()},
            ),
            build_system=tuple(self.build_system),
            provisioned=tuple(r.to_request() for r in self.provisioned),
        )


class AutomaticDecisionDoc(BaseModel):
    gate: str
    decision: str
    artifact_sha256: str
    reasons: list[str]


class JiraReportDoc(BaseModel):
    status: str
    outcome: str


AUTOMATIC_ACTOR = "policy:automatic"
# Codes of transient infrastructure failures (Git host or network), never code defects.
INFRA_CODES = frozenset({"provider_unavailable", "rate_limited", "provider_timeout"})
# Validation could not run the checks at all (Docker gone, a container that never started, the
# sandbox or workspace runtime failing): retryable, and never spent on a repair run. A command
# timeout is deliberately absent: it is the profile's per-command budget, most often exceeded by the
# change itself (a hang, or a call blocking under --network none), so it stays a repairable failure.
VALIDATION_INFRA_CODES = INFRA_CODES | {
    "docker_unavailable",
    "container_start_failed",
    "workspace_runtime_failed",
    "sandbox_start_failed",
}
_RETRYABLE = (
    "timeout",
    "unavailable",
    "rate_limited",
    "agent_error",
    "agent_output",
    "connection",
    "outcome",
)
_STAGES = (
    ("dependency", "dependency preparation"),
    ("agent", "agent"),
    ("validation", "validation"),
    ("delivery", "delivery"),
    ("publish", "delivery"),
    ("repository", "repository authorisation"),
)


class DeliveryResultDoc(BaseModel):
    outcome: str
    pr_url: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    failure_code: str | None = None


@dataclass
class StepDependencies:
    changes: CodeChangeService
    registry_uow: Callable[[], RegistryUnitOfWork]
    profiles: ProfileReader
    baselines: RegistryBaselineProvider
    runtime: Runtime
    sandboxes: SandboxStarter
    agent: Agent
    engine: ValidationEngine
    artifacts: ScopedArtifacts
    jira: JiraIssueReader
    publishers: DeliveryPublishers
    reporter: Reporter
    delivery_enabled: bool = False
    pr_create_enabled: bool = False
    max_repairs: int = 1
    toolchains: frozenset[str] = frozenset({"python"})  # families with an execution image
    agent_checkpointer: Any = None
    max_changed_file_bytes: int = 1024 * 1024
    dependencies: DependencyPreparer | None = None
    approval_mode: str = "manual"  # composition passes WORKFLOW_APPROVAL_MODE (default automatic)
    max_dependency_packages: int = 30
    dependency_index_url: str = "https://pypi.org/simple"  # the only index a manifest may name
    multirepo: Any = None  # Coordinator: child context and parent-level Jira reporting
    infra_retry_delays: tuple[float, ...] = (2.0, 8.0)  # transient Git host failures


def dependency_preparation(profile: RepositoryProfile, toolchains: frozenset[str]) -> str | None:
    """None when the sandbox can run this repository offline; otherwise the BLOCKED reason.
    Network access is never enabled to work around it."""
    if any(t not in toolchains for t in profile.toolchains) or not profile.toolchains:
        return "no_execution_image_for_toolchain"
    if any(c.network != "none" or c.kind is CommandKind.INSTALL for c in profile.commands):
        return "dependency_preparation_requires_network"
    return None


def _code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return str(code) if isinstance(code, str) and code else type(exc).__name__.lower()[:64]


class CodeChangeSteps:
    def __init__(self, deps: StepDependencies) -> None:
        self._d = deps

    # ----- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _scope(a: CodeChangeAttempt) -> ArtifactScope:
        return ArtifactScope(a.request_id, a.id)

    async def _attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt:
        return await self._d.changes.get_attempt(attempt_id)

    async def _fail(self, attempt_id: uuid.UUID, code: str) -> StepResult:
        try:
            a = await self._d.changes.transition(attempt_id, S.FAILED, failure_code=code[:64])
        except InvalidTransitionError:
            a = await self._attempt(attempt_id)
        _log.warning("attempt_failed", attempt_id=str(attempt_id), code=code)
        return StepResult(a.status.value, failure_code=code)

    async def _story(self, a: CodeChangeAttempt) -> StoryContext:
        request = await self._d.changes.get_request(a.request_id)
        issue = await self._d.jira.get_issue(request.jira_issue_key)
        story = story_of(issue)
        loaded = await self._d.profiles.get(a.profile_version_id)
        assert a.base_commit_sha is not None  # noqa: S101 - pinned before planning
        brief = None
        if self._d.multirepo is not None:
            ctx = await self._d.multirepo.context_for(a.id)
            if ctx is not None:
                brief = MultiRepoBrief(
                    role=ctx.role,
                    responsibility=ctx.responsibility,
                    siblings=ctx.siblings,
                    downstream=ctx.downstream,
                    upstream_contracts=tuple(
                        (c.repository_key, c.sha256, c.text) for c in ctx.upstream_contracts
                    ),
                )
        return StoryContext(
            issue_key=request.jira_issue_key,  # regex-validated, unlike the key Jira returned
            summary=story.summary,
            description=story.description,
            acceptance_criteria=story.acceptance_criteria,
            repository_key=loaded.profile.repository_key,
            base_commit_sha=a.base_commit_sha,
            validation_commands=tuple(" ".join(c.argv) for c in loaded.profile.commands),
            multi_repo=brief,
        )

    @asynccontextmanager
    async def _workspace(
        self, a: CodeChangeAttempt, *, writable: bool, dependency_dir: str | None = None
    ) -> AsyncIterator[Any]:
        assert a.base_commit_sha is not None  # noqa: S101
        source = await self._d.baselines.baseline_for(
            repository_id=a.repository_id,
            profile_version_id=a.profile_version_id,
            base_commit_sha=a.base_commit_sha,
        )
        spec = WorkspaceSpec(
            a.request_id,
            a.id,
            a.repository_id,
            a.profile_version_id,
            a.base_commit_sha,
            dependency_dir,
        )
        await self._d.runtime.destroy_attempt_workspaces(a.id)  # crash recovery (same attempt)
        handle = await self._d.runtime.create(spec, source)
        profile = (await self._d.profiles.get(a.profile_version_id)).profile
        toolchain = profile.toolchains[0] if profile.toolchains else "python"
        sandbox = None
        try:
            sandbox = await self._d.sandboxes.start(
                handle,
                self._d.runtime.tree_path(handle, "work"),
                writable=writable,
                toolchain=toolchain,  # the Python execution image
            )
            yield handle, sandbox
        finally:
            if sandbox is not None:
                await self._d.sandboxes.stop(sandbox)
            await self._d.runtime.destroy(handle)

    async def _latest_optional(
        self, a: CodeChangeAttempt, kind: ArtifactKind
    ) -> tuple[str, str] | None:
        metas = [m for m in await self._d.artifacts.list(self._scope(a)) if m.kind is kind]
        if not metas:
            return None
        latest = max(metas, key=lambda m: (m.created_at, m.id))
        return latest.id, latest.sha256

    async def _latest(self, a: CodeChangeAttempt, kind: ArtifactKind) -> tuple[str, str]:
        latest = await self._latest_optional(a, kind)
        if latest is None:
            raise InvalidTransitionError(f"{kind.value}_missing")
        return latest

    async def _discovered(self, a: CodeChangeAttempt) -> ManifestDiscoveryResult:
        latest = await self._latest_optional(a, ArtifactKind.REPOSITORY_DEPENDENCIES)
        if latest is None:
            return ManifestDiscoveryResult()
        doc = await load_verified(
            self._d.artifacts, self._scope(a), latest[0], latest[1], RepositoryDependenciesDoc
        )
        return doc.to_result()

    # ----- steps -----------------------------------------------------------------------------
    async def prepare(self, attempt_id: uuid.UUID) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is S.PENDING:
            a = await self._d.changes.transition(attempt_id, S.PREPARING)
        if a.status is not S.PREPARING:
            return StepResult(a.status.value)
        try:
            loaded = await self._d.profiles.get(a.profile_version_id)
            blocked = dependency_preparation(loaded.profile, self._d.toolchains)
            if blocked:
                a = await self._d.changes.transition(attempt_id, S.BLOCKED, failure_code=blocked)
                return StepResult(a.status.value, failure_code=blocked)
            if a.base_commit_sha is None:
                sha = await self._d.baselines.pin(
                    repository_id=a.repository_id, profile_version_id=a.profile_version_id
                )
                await self._d.changes.pin_baseline(attempt_id, sha)
            a = await self._d.changes.transition(attempt_id, S.PLANNING)
            return StepResult(a.status.value)
        except Exception as exc:
            return await self._fail(attempt_id, _code(exc))

    async def plan(self, attempt_id: uuid.UUID) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is not S.PLANNING:
            return StepResult(a.status.value)
        try:
            story = await self._story(a)
            async with self._workspace(a, writable=False) as (handle, sandbox):
                discovered = await asyncio.to_thread(
                    discover_repository_dependencies,
                    self._d.runtime.tree_path(handle, "baseline"),
                    max_packages=self._d.max_dependency_packages,
                    expected_index_url=self._d.dependency_index_url,
                )
                if discovered.provisioned:
                    story = replace(
                        story,
                        repository_dependencies=tuple(
                            r.requirement() for r in discovered.provisioned
                        ),
                    )
                plan = await self._d.agent.plan(
                    story,
                    sandbox,
                    thread_id=checked_thread_id(  # fresh per run
                        f"attempt:{a.id}:planning:{uuid.uuid4().hex[:8]}"
                    ),
                    checkpointer=self._d.agent_checkpointer,
                )
            await store_model(
                self._d.artifacts,
                self._scope(a),
                ArtifactKind.REPOSITORY_DEPENDENCIES,
                RepositoryDependenciesDoc.from_result(discovered),
            )
            stored = await store_model(
                self._d.artifacts, self._scope(a), ArtifactKind.IMPLEMENTATION_PLAN, plan
            )
            await self._d.changes.record_plan(attempt_id, stored.ref, stored.sha256)
            a = await self._d.changes.transition(attempt_id, S.AWAITING_PLAN_APPROVAL)
            return StepResult(a.status.value)
        except Exception as exc:
            return await self._fail(attempt_id, _code(exc))

    async def gate(self, attempt_id: uuid.UUID, gate: str) -> GateState:
        a = await self._attempt(attempt_id)
        if a.status is S.REJECTED:
            return GateState.REJECTED
        if a.valid_approval(ApprovalGate(gate), await self._d.changes.approvals(attempt_id)):
            return GateState.APPROVED
        if a.status in TERMINAL:
            return GateState.REJECTED
        waiting = S.AWAITING_PLAN_APPROVAL if gate == "PLAN" else S.AWAITING_FINAL_APPROVAL
        if self._d.approval_mode == "automatic" and a.status is waiting:
            return await self._decide_automatically(a, ApprovalGate(gate))
        return GateState.PENDING

    async def _decide_automatically(self, a: CodeChangeAttempt, gate: ApprovalGate) -> GateState:
        """Deterministic policy instead of a person: the decision is recorded exactly like an
        operator's (bound to the artifact hash, actor policy:automatic) with its reasons."""
        artifact = a.plan_sha256 if gate is ApprovalGate.PLAN else a.manifest_sha256
        assert artifact is not None  # noqa: S101 - set before the attempt waits at this gate
        reasons = await (
            self._plan_policy(a) if gate is ApprovalGate.PLAN else self._final_policy(a)
        )
        decision = ApprovalDecision.REJECTED if reasons else ApprovalDecision.APPROVED
        await store_model(
            self._d.artifacts,
            self._scope(a),
            ArtifactKind.AUTOMATIC_DECISION,
            AutomaticDecisionDoc(
                gate=gate.value, decision=decision.value, artifact_sha256=artifact, reasons=reasons
            ),
        )
        await self._d.changes.decide(a.id, gate, artifact, decision, AUTOMATIC_ACTOR)
        _log.info("automatic_decision", attempt_id=str(a.id), gate=gate.value, reasons=reasons)
        return GateState.REJECTED if reasons else GateState.APPROVED

    async def _plan_policy(self, a: CodeChangeAttempt) -> list[str]:
        assert a.plan_ref and a.plan_sha256  # noqa: S101
        plan = await load_verified(
            self._d.artifacts, self._scope(a), a.plan_ref, a.plan_sha256, ImplementationPlan
        )
        discovered = await self._discovered(a)
        profile = (await self._d.profiles.get(a.profile_version_id)).profile
        reasons: list[str] = []
        try:
            merge_dependency_requests(
                discovered, plan.dependencies, max_packages=self._d.max_dependency_packages
            )
        except DependencyPolicyError as exc:
            reasons.append(exc.code)
        if len(plan.changes) > profile.limits.max_files_changed:
            reasons.append("plan_exceeds_changed_file_limit")
        for change in plan.changes:
            try:
                path = validate_relative_path(change.path)
            except Exception:
                reasons.append("plan_path_invalid")
                continue
            if is_protected(path):
                reasons.append(f"plan_touches_protected_path:{path[:120]}")
        if not any(c.kind is CommandKind.TEST for c in profile.commands):
            reasons.append("profile_defines_no_test_command")
        return reasons

    async def _final_policy(self, a: CodeChangeAttempt) -> list[str]:
        assert a.manifest_ref and a.manifest_sha256  # noqa: S101
        scope = self._scope(a)
        manifest = await load_verified(
            self._d.artifacts, scope, a.manifest_ref, a.manifest_sha256, DeliveryManifest
        )
        report = await load_verified(
            self._d.artifacts,
            scope,
            manifest.validation_report_ref,
            manifest.validation_report_sha256,
            ValidationReport,
        )
        reasons: list[str] = []
        if not report.passed:
            reasons.append("independent_validation_not_passed")
        if manifest.plan_sha256 != a.plan_sha256:
            reasons.append("manifest_plan_mismatch")
        tests = [c for c in report.commands if c.kind == "test"]
        if not tests or any(c.status != "PASSED" for c in tests):
            reasons.append("tests_not_executed_and_passed")
        return reasons

    async def _bundle(
        self, plan: ImplementationPlan, discovered: ManifestDiscoveryResult
    ) -> DependencyBundle | None:
        requests = merge_dependency_requests(
            discovered, plan.dependencies, max_packages=self._d.max_dependency_packages
        )
        if not requests:
            return None
        if self._d.dependencies is None:
            raise DependencyPolicyError("dependency_preparation_unavailable")
        return await self._d.dependencies.prepare(requests)

    async def implement(self, attempt_id: uuid.UUID, feedback: str | None) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is S.AWAITING_PLAN_APPROVAL:
            a = await self._d.changes.transition(attempt_id, S.IMPLEMENTING)  # re-checks approval
        if a.status is not S.IMPLEMENTING:
            return StepResult(a.status.value)
        try:
            assert a.plan_ref and a.plan_sha256 and a.base_commit_sha  # noqa: S101
            plan = await load_verified(
                self._d.artifacts, self._scope(a), a.plan_ref, a.plan_sha256, ImplementationPlan
            )
            discovered = await self._discovered(a)
            story = await self._story(a)
            bundle = await self._bundle(plan, discovered)
            if bundle is not None:
                await store_model(
                    self._d.artifacts,
                    self._scope(a),
                    ArtifactKind.DEPENDENCY_LOCK,
                    DependencyLockDoc(
                        key=bundle.key,
                        lock_sha256=bundle.lock_sha256,
                        packages=[p.__dict__ for p in bundle.packages],
                        sources=list(discovered.sources),
                        categories={
                            "runtime": [r.requirement() for r in discovered.categories.runtime],
                            "test_dev": [r.requirement() for r in discovered.categories.test_dev],
                            "plan": list(plan.dependencies),
                        },
                    ),
                )
            elif await self._latest_optional(a, ArtifactKind.DEPENDENCY_LOCK) is not None:
                raise DependencyPolicyError("dependency_bundle_changed")
            async with self._workspace(
                a, writable=True, dependency_dir=bundle.site_dir if bundle else None
            ) as (handle, sandbox):
                report = await self._d.agent.implement(
                    story,
                    plan,
                    sandbox,
                    feedback=feedback,
                    thread_id=checked_thread_id(  # a re-run after a crash starts clean
                        f"attempt:{a.id}:implementation:{a.repair_count}:{uuid.uuid4().hex[:8]}"
                    ),
                    checkpointer=self._d.agent_checkpointer,
                )
                if sandbox.disk_limit_exceeded:
                    raise WorkspaceLimitError
                ops = changed_paths(
                    await self._d.runtime.manifest(handle, "baseline"),
                    await self._d.runtime.manifest(handle, "work"),
                )
                contents = {
                    p: await self._d.runtime.read_file(
                        handle, p, tree="work", max_bytes=self._d.max_changed_file_bytes
                    )
                    for p, op in ops.items()
                    if op.value != "delete"
                }
                changes = changeset(ops, contents)
            scope = self._scope(a)
            await store_model(self._d.artifacts, scope, ArtifactKind.IMPLEMENTATION_REPORT, report)
            await store_model(
                self._d.artifacts,
                scope,
                ArtifactKind.CHANGESET,
                ChangesetDocument.of(a.base_commit_sha, changes),
            )
            a = await self._d.changes.transition(attempt_id, S.VALIDATING)
            return StepResult(a.status.value)
        except Exception as exc:
            return await self._fail(attempt_id, _code(exc))

    async def validate(self, attempt_id: uuid.UUID) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is not S.VALIDATING:
            return StepResult(a.status.value)
        try:
            assert a.plan_ref and a.plan_sha256 and a.base_commit_sha  # noqa: S101
            scope = self._scope(a)
            plan = await load_verified(
                self._d.artifacts, scope, a.plan_ref, a.plan_sha256, ImplementationPlan
            )
            cs_ref, cs_sha = await self._latest(a, ArtifactKind.CHANGESET)
            doc = await load_verified(self._d.artifacts, scope, cs_ref, cs_sha, ChangesetDocument)
            changes = doc.changes()
            loaded = await self._d.profiles.get(a.profile_version_id)
            commit = a.base_commit_sha
            discovered = await self._discovered(a)
            bundle = await self._bundle(plan, discovered)  # the same verified bundle (cache hit)
            if bundle is not None:  # validation must use exactly what the agent worked with
                lock_ref, lock_sha = await self._latest(a, ArtifactKind.DEPENDENCY_LOCK)
                used = await load_verified(
                    self._d.artifacts, scope, lock_ref, lock_sha, DependencyLockDoc
                )
                if used.lock_sha256 != bundle.lock_sha256:
                    raise DependencyPolicyError("dependency_bundle_changed")
            elif await self._latest_optional(a, ArtifactKind.DEPENDENCY_LOCK) is not None:
                raise DependencyPolicyError("dependency_bundle_changed")

            async def fresh_baseline() -> Any:
                # Transient Git host failures (5xx, 429, timeouts) are retried with backoff here,
                # so they never reach the agent as a "code defect" (no repair run is spent).
                for delay in (*self._d.infra_retry_delays, None):
                    try:
                        return await self._d.baselines.baseline_for(
                            repository_id=a.repository_id,
                            profile_version_id=a.profile_version_id,
                            base_commit_sha=commit,
                        )
                    except Exception as exc:
                        if delay is None or _code(exc) not in INFRA_CODES:
                            raise
                        _log.warning(
                            "validation_baseline_retry", attempt_id=str(a.id), code=_code(exc)
                        )
                        await asyncio.sleep(delay)
                raise AssertionError("unreachable")  # pragma: no cover

            report = await self._d.engine.validate(
                ValidationInput(
                    request_id=a.request_id,
                    attempt_id=a.id,
                    repository_id=a.repository_id,
                    profile_version_id=a.profile_version_id,
                    base_commit_sha=commit,
                    profile=loaded.profile,
                    plan=plan,
                    changes=changes,
                    baseline=fresh_baseline,
                    validation_policy=loaded.profile.validation_policy,
                    dependency_dir=bundle.site_dir if bundle else None,
                )
            )
            stored = await store_model(
                self._d.artifacts, scope, ArtifactKind.VALIDATION_REPORT, report
            )
            if not report.passed:
                failed = [c for c in report.checks if c.mandatory and c.status.value == "FAILED"]
                reasons = {c.reason for c in report.commands if c.reason}
                # Precedence: static findings never co-occur with these (the engine skips every
                # command when a static check fails), and infrastructure wins over a command that
                # failed in the same run, so the one repair is not spent on broken Docker.
                if any((c.detail or "") in VALIDATION_INFRA_CODES for c in failed) or (
                    reasons & VALIDATION_INFRA_CODES
                ):
                    # an infrastructure failure is not a code defect: no repair run
                    return await self._fail(attempt_id, "validation_infrastructure_unavailable")
                if "command_not_found" in reasons:
                    # the agent cannot install tools in the network-less sandbox: no repair run
                    return await self._fail(attempt_id, "validation_command_not_found")
                if a.repair_count < self._d.max_repairs:
                    a = await self._d.changes.transition(attempt_id, S.IMPLEMENTING)
                    return StepResult(a.status.value, feedback=report.feedback())
                return await self._fail(attempt_id, "validation_failed")
            manifest = await self._manifest(a, cs_ref, cs_sha, stored.ref, stored.sha256, changes)
            m = await store_model(
                self._d.artifacts, scope, ArtifactKind.DELIVERY_MANIFEST, manifest
            )
            await self._d.changes.record_manifest(attempt_id, m.ref, m.sha256)
            a = await self._d.changes.transition(attempt_id, S.AWAITING_FINAL_APPROVAL)
            return StepResult(a.status.value)
        except Exception as exc:
            return await self._fail(attempt_id, _code(exc))

    async def _manifest(
        self,
        a: CodeChangeAttempt,
        cs_ref: str,
        cs_sha: str,
        report_ref: str,
        report_sha: str,
        changes: Any,
    ) -> DeliveryManifest:
        assert a.base_commit_sha and a.plan_sha256  # noqa: S101
        request = await self._d.changes.get_request(a.request_id)
        baseline = await self._d.baselines.read(
            repository_id=a.repository_id,
            profile_version_id=a.profile_version_id,
            base_commit_sha=a.base_commit_sha,
        )
        result_hash = expected_manifest(_tree(baseline), changes).tree_hash
        return DeliveryManifest(
            issue_key=request.jira_issue_key,
            request_id=str(a.request_id),
            attempt_id=str(a.id),
            attempt_number=a.number,
            repository_id=str(a.repository_id),
            base_commit_sha=a.base_commit_sha,
            plan_sha256=a.plan_sha256,
            changeset_ref=cs_ref,
            changeset_sha256=cs_sha,
            validation_report_ref=report_ref,
            validation_report_sha256=report_sha,
            result_tree_hash=result_hash,
            branch=branch_name(request.jira_issue_key, a.number, cs_sha),
            commit_message=commit_message(
                issue_key=request.jira_issue_key,
                attempt_number=a.number,
                base_commit_sha=a.base_commit_sha,
                plan_sha256=a.plan_sha256,
                changeset_sha256=cs_sha,
                validation_report_sha256=report_sha,
            ),
            commit_time=datetime.now(UTC).replace(microsecond=0),
        )

    async def deliver(self, attempt_id: uuid.UUID) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is S.AWAITING_FINAL_APPROVAL:
            if not (self._d.delivery_enabled and self._d.pr_create_enabled):
                return StepResult("DELIVERY_DISABLED")  # stays approved and waiting
            a = await self._d.changes.transition(attempt_id, S.DELIVERING)  # re-checks approval
        if a.status is not S.DELIVERING:
            return StepResult(a.status.value)
        try:
            return await self._publish(a)
        except Exception as exc:
            return await self._fail(attempt_id, _code(exc))

    async def _publish(self, a: CodeChangeAttempt) -> StepResult:
        assert a.manifest_ref and a.manifest_sha256  # noqa: S101
        scope = self._scope(a)
        manifest = await load_verified(
            self._d.artifacts, scope, a.manifest_ref, a.manifest_sha256, DeliveryManifest
        )
        report = await load_verified(
            self._d.artifacts,
            scope,
            manifest.validation_report_ref,
            manifest.validation_report_sha256,
            ValidationReport,
        )
        doc = await load_verified(
            self._d.artifacts,
            scope,
            manifest.changeset_ref,
            manifest.changeset_sha256,
            ChangesetDocument,
        )
        request = await self._d.changes.get_request(a.request_id)
        async with self._d.registry_uow() as uow:
            repo = await uow.store.get_repository_by_id(a.repository_id)
            mappings = await uow.store.list_mappings(
                jira_project_key=request.jira_project_key,
                repository_id=a.repository_id,
                status=MappingStatus.ACTIVE,
            )
        if repo is None or repo.status is not RepositoryStatus.ACTIVE or not mappings:
            return await self._fail(a.id, "repository_not_authorised")
        baseline = await self._d.baselines.read(  # re-verified (and installation re-checked)
            repository_id=a.repository_id,
            profile_version_id=a.profile_version_id,
            base_commit_sha=manifest.base_commit_sha,
        )
        publisher = self._d.publishers.get(repo.git_provider)
        if publisher is None:
            return await self._fail(a.id, "publisher_unavailable")
        receipt = await publisher.publish(
            DeliveryRequest(
                target=DeliveryTarget(
                    repo.id,
                    repo.git_provider,
                    repo.provider_repository_id,
                    repo.owner,
                    repo.name,
                    repo.default_branch,
                ),
                base_commit_sha=manifest.base_commit_sha,
                baseline_tree_hash=baseline.tree_hash,
                result_tree_hash=manifest.result_tree_hash,
                branch=manifest.branch,
                changes=doc.changes(),
                commit_message=manifest.commit_message,
                commit_time=manifest.commit_time,
                manifest_sha256=a.manifest_sha256,
                baseline=tuple(
                    BaselineEntry(f.path, f.executable, hashlib.sha256(f.content).hexdigest())
                    for f in baseline.files
                ),
                pull_request=build_pull_request(manifest, report),
            )
        )
        await store_model(
            self._d.artifacts,
            scope,
            ArtifactKind.DELIVERY_RESULT,
            DeliveryResultDoc(
                outcome=receipt.outcome.value,
                pr_url=receipt.pr_url,
                pr_number=receipt.pr_number,
                commit_sha=receipt.commit_sha,
                failure_code=receipt.failure_code,
            ),
        )
        if receipt.outcome is DeliveryOutcome.PULL_REQUEST_CREATED:
            a = await self._d.changes.transition(a.id, S.REPORTING)
            return StepResult(a.status.value)
        if receipt.outcome is DeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN:
            return StepResult(S.DELIVERING.value, failure_code=receipt.failure_code)  # reconcile
        return await self._fail(a.id, receipt.failure_code or "delivery_failed")

    async def report(self, attempt_id: uuid.UUID) -> StepResult:
        a = await self._attempt(attempt_id)
        if a.status is not S.REPORTING:
            return StepResult(a.status.value)
        request = await self._d.changes.get_request(a.request_id)
        scope = self._scope(a)
        ref, sha = await self._latest(a, ArtifactKind.DELIVERY_RESULT)
        result = await load_verified(self._d.artifacts, scope, ref, sha, DeliveryResultDoc)
        assert a.manifest_ref and a.manifest_sha256 and a.plan_ref and a.plan_sha256  # noqa: S101
        manifest = await load_verified(
            self._d.artifacts, scope, a.manifest_ref, a.manifest_sha256, DeliveryManifest
        )
        validation = await load_verified(
            self._d.artifacts,
            scope,
            manifest.validation_report_ref,
            manifest.validation_report_sha256,
            ValidationReport,
        )
        plan = await load_verified(
            self._d.artifacts, scope, a.plan_ref, a.plan_sha256, ImplementationPlan
        )
        commands = ", ".join(f"{c.command_id} {c.status}" for c in validation.commands) or "none"
        text = (
            f"AI Code Change Agent created a draft pull request for {request.jira_issue_key}.\n"
            f"Repository: {await self._repository_name(a)}\n"
            f"Branch: {manifest.branch}\n"
            f"Draft PR: {result.pr_url}\n"
            f"Summary: {plan.summary[:600]}\n"
            f"Tests and checks run by independent validation: {commands}\n"
            f"Validation outcome: PASSED. Attempt {a.number} ({a.id}).\n"
            "Review and merge are human decisions."
        )
        await self._report_once(a, request.jira_issue_key, text)
        a = await self._d.changes.transition(attempt_id, S.COMPLETED)
        return StepResult(a.status.value)

    async def cleanup(self, attempt_id: uuid.UUID) -> None:
        """Workspaces are destroyed by the step that created them. An attempt that ended without
        a pull request is reported to Jira once."""
        a = await self._attempt(attempt_id)
        if a.status in TERMINAL and a.status is not S.COMPLETED:
            try:
                request = await self._d.changes.get_request(a.request_id)
                await self._report_once(a, request.jira_issue_key, await self._failure_text(a))
            except Exception as exc:  # reporting never changes the outcome
                _log.warning("jira_report_failed", attempt_id=str(a.id), code=_code(exc))
        _log.info("workflow_finished", attempt_id=str(attempt_id))

    async def _repository_name(self, a: CodeChangeAttempt) -> str:
        async with self._d.registry_uow() as uow:
            repo = await uow.store.get_repository_by_id(a.repository_id)
        return f"{repo.owner}/{repo.name}" if repo else str(a.repository_id)

    async def _failure_text(self, a: CodeChangeAttempt) -> str:
        reason = a.failure_code or a.status.value.lower()
        decisions = [
            m
            for m in await self._d.artifacts.list(self._scope(a))
            if m.kind is ArtifactKind.AUTOMATIC_DECISION
        ]
        if a.status is S.REJECTED and decisions:
            latest = max(decisions, key=lambda m: (m.created_at, m.id))
            doc = await load_verified(
                self._d.artifacts, self._scope(a), latest.id, latest.sha256, AutomaticDecisionDoc
            )
            reason = f"{doc.gate} policy rejected: " + "; ".join(doc.reasons)[:600]
        stage = {
            S.BLOCKED: "preparation",
            S.REJECTED: "automatic policy gate",
        }.get(a.status) or next((s for p, s in _STAGES if reason.startswith(p)), "workflow")
        retryable = any(word in reason for word in _RETRYABLE)
        advice = (
            "yes, after the temporary cause is resolved"
            if retryable
            else "no, the cause must be fixed first"
        )
        request = await self._d.changes.get_request(a.request_id)
        return (
            f"AI Code Change Agent could not complete {request.jira_issue_key}.\n"
            f"Repository: {await self._repository_name(a)}\n"
            f"Outcome: {a.status.value} at the {stage} stage (attempt {a.number}, {a.id}).\n"
            f"Reason: {reason}\n"
            f"Retryable: {advice}.\n"
            "No pull request was created by this attempt."
        )

    async def _report_once(self, a: CodeChangeAttempt, issue_key: str, text: str) -> None:
        """One Jira comment per attempt outcome: a JIRA_REPORT artifact marks it as settled, either
        posted or permanently failed (PERMANENT_FAILURE: Jira refused it with 400/401/403/404 or
        similar, so posting again cannot help). Only a retryable failure (FAILED) stores nothing,
        so a later cleanup may try again. Reporting never changes or re-runs the attempt. A
        multi-repository child reports nothing here: its parent posts one aggregated comment."""
        if self._d.multirepo is not None and await self._d.multirepo.is_child(a.id):
            return
        scope = self._scope(a)
        if any(m.kind is ArtifactKind.JIRA_REPORT for m in await self._d.artifacts.list(scope)):
            return
        outcome = await self._d.reporter.report(issue_key, text)
        _log.info("jira_report", attempt_id=str(a.id), outcome=str(outcome))
        if str(outcome) == "PERMANENT_FAILURE":
            _log.warning("jira_report_permanent_failure", attempt_id=str(a.id))
        if str(outcome) != "FAILED":
            await store_model(
                self._d.artifacts,
                scope,
                ArtifactKind.JIRA_REPORT,
                JiraReportDoc(status=a.status.value, outcome=str(outcome)),
            )


def _tree(baseline: VerifiedBaseline) -> TreeManifest:
    return TreeManifest(
        tuple(
            TreeEntry(f.path, len(f.content), hashlib.sha256(f.content).hexdigest(), f.executable)
            for f in baseline.files
        )
    )
