"""The independent Python validation engine (not an AI agent).

Input: the pinned baseline, the changeset the platform extracted from the implementation
sandbox, the approved plan and the published repository profile. It never trusts the agent's
report. It rebuilds a FRESH workspace from the baseline, applies the changeset, proves the
rebuilt tree is exactly baseline + changes, runs the profile's validation commands in a new
network-less container, and decides PR eligibility. The fresh workspace is always destroyed.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.workspaces.errors import WorkspaceError
from ai_code_change_agent.application.workspaces.models import (
    ChangeLimits,
    FileChange,
    TreeManifest,
    WorkspaceHandle,
    WorkspaceSpec,
)
from ai_code_change_agent.application.workspaces.ports import BaselineSource, WorkspaceRuntime
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.validation import rules
from ai_code_change_agent.validation.decision import commands_check, decide, required_tests
from ai_code_change_agent.validation.git_inspector import changeset_sha256, expected_manifest
from ai_code_change_agent.validation.schemas import (
    ChangedFile,
    CheckKind,
    CheckRecord,
    CheckStatus,
    CommandRecord,
    ValidationReport,
)
from ai_code_change_agent.validation.security_scanner import secrets

BaselineFactory = Callable[[], Awaitable[BaselineSource]]


@dataclass(frozen=True)
class ValidationInput:
    request_id: uuid.UUID
    attempt_id: uuid.UUID
    repository_id: uuid.UUID
    profile_version_id: uuid.UUID
    base_commit_sha: str
    profile: RepositoryProfile
    plan: ImplementationPlan
    changes: tuple[FileChange, ...]
    baseline: BaselineFactory  # a fresh, verified source of the pinned baseline
    validation_policy: str = "standard"
    dependency_dir: str | None = None  # the attempt's verified dependency bundle (read-only)


def _changed(changes: tuple[FileChange, ...]) -> list[ChangedFile]:
    import hashlib

    return [
        ChangedFile(
            path=c.path,
            operation=c.op.value,
            size=len(c.content or b""),
            sha256=hashlib.sha256(c.content).hexdigest() if c.content is not None else None,
        )
        for c in changes
    ]


class ValidationEngine:
    def __init__(self, runtime: WorkspaceRuntime, policy: CommandPolicy | None = None) -> None:
        self._runtime = runtime
        self._policy = policy or CommandPolicy()

    async def validate(self, data: ValidationInput) -> ValidationReport:
        changes = data.changes
        checks: list[CheckRecord] = [
            rules.changeset_present(changes),
            rules.path_safety(changes),
            rules.protected_paths(changes),
            rules.plan_scope(changes, data.plan),
            rules.change_limits(changes, data.profile),
            secrets(changes),
        ]
        commands: list[CommandRecord] = []
        static_ok = all(c.status is CheckStatus.PASSED for c in checks)
        if static_ok:
            rebuild, commands = await self._fresh_run(data)
            checks.append(rebuild)
            if rebuild.status is CheckStatus.PASSED:
                checks += [
                    required_tests(commands),
                    commands_check(commands, data.validation_policy),
                ]
        else:  # nothing is executed for a changeset that already fails a static rule
            for kind in (CheckKind.REBUILD_INTEGRITY, CheckKind.REQUIRED_TESTS, CheckKind.COMMANDS):
                checks.append(
                    CheckRecord(
                        kind=kind, status=CheckStatus.SKIPPED, detail="static_checks_failed"
                    )
                )
        checks.append(self._acceptance(changes, data.plan, commands))
        decision, reasons = decide(checks)
        return ValidationReport(
            decision=decision,
            base_commit_sha=data.base_commit_sha,
            validation_policy=data.validation_policy,
            changeset_sha256=changeset_sha256(changes),
            changed_files=_changed(changes),
            checks=checks,
            commands=commands,
            reasons=reasons,
        )

    async def _fresh_run(self, data: ValidationInput) -> tuple[CheckRecord, list[CommandRecord]]:
        handle: WorkspaceHandle | None = None
        try:
            spec = WorkspaceSpec(
                data.request_id,
                data.attempt_id,
                data.repository_id,
                data.profile_version_id,
                data.base_commit_sha,
                data.dependency_dir,
            )
            handle = await self._runtime.create(spec, await data.baseline())
            baseline: TreeManifest = await self._runtime.manifest(handle, "baseline")
            await self._runtime.apply_changes(
                handle,
                data.changes,
                ChangeLimits(
                    max_files=data.profile.limits.max_files_changed,
                    max_total_bytes=data.profile.limits.max_diff_bytes,
                    max_file_bytes=data.profile.limits.max_diff_bytes,
                ),
            )
            rebuilt = await self._runtime.manifest(handle, "work")
            if rebuilt.tree_hash != expected_manifest(baseline, data.changes).tree_hash:
                return CheckRecord(
                    kind=CheckKind.REBUILD_INTEGRITY,
                    status=CheckStatus.FAILED,
                    detail="rebuilt_tree_differs",
                ), []
            from ai_code_change_agent.validation.test_runner import run_commands

            commands = await run_commands(self._runtime, handle, data.profile, self._policy)
            return CheckRecord(
                kind=CheckKind.REBUILD_INTEGRITY, status=CheckStatus.PASSED
            ), commands
        except WorkspaceError as exc:
            return CheckRecord(
                kind=CheckKind.REBUILD_INTEGRITY, status=CheckStatus.FAILED, detail=str(exc)[:200]
            ), []
        finally:
            if handle is not None:
                await self._runtime.destroy(handle)

    @staticmethod
    def _acceptance(
        changes: tuple[FileChange, ...], plan: ImplementationPlan, commands: list[CommandRecord]
    ) -> CheckRecord:
        """Informational: did the change touch the tests the plan promised, and did tests run?
        Whether acceptance criteria are truly met is a human judgement at the final gate."""
        touched = {c.path for c in changes} & set(plan.tests)
        ran = any(c.kind == "test" and c.status == "PASSED" for c in commands)
        detail = (
            f"{len(plan.acceptance_criteria)} criteria; planned tests touched: "
            f"{len(touched)}/{len(plan.tests)}; tests passed: {ran}"
        )
        return CheckRecord(
            kind=CheckKind.ACCEPTANCE_EVIDENCE,
            status=CheckStatus.INFO,
            detail=detail,
            mandatory=False,
        )
