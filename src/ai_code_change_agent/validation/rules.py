"""Deterministic changeset rules (no model involvement)."""

from collections.abc import Sequence

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.workspaces.errors import UnsafePathError
from ai_code_change_agent.application.workspaces.models import ChangeOp, FileChange
from ai_code_change_agent.application.workspaces.paths import is_protected, validate_relative_path
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.validation.schemas import CheckKind, CheckRecord, CheckStatus

P, F = CheckStatus.PASSED, CheckStatus.FAILED


def _list(paths: Sequence[str]) -> str:
    return ", ".join(sorted(paths)[:10])


def changeset_present(changes: Sequence[FileChange]) -> CheckRecord:
    return CheckRecord(
        kind=CheckKind.CHANGESET_PRESENT,
        status=P if changes else F,
        detail=None if changes else "no_changes",
    )


def path_safety(changes: Sequence[FileChange]) -> CheckRecord:
    bad = []
    for c in changes:
        try:
            if validate_relative_path(c.path) != c.path:
                bad.append(c.path)
        except UnsafePathError:
            bad.append(c.path)
    return CheckRecord(
        kind=CheckKind.PATH_SAFETY,
        status=F if bad else P,
        detail=f"unsafe paths: {_list(bad)}" if bad else None,
    )


def protected_paths(changes: Sequence[FileChange]) -> CheckRecord:
    bad = [c.path for c in changes if is_protected(c.path)]
    return CheckRecord(
        kind=CheckKind.PROTECTED_PATHS,
        status=F if bad else P,
        detail=f"protected paths changed: {_list(bad)}" if bad else None,
    )


def plan_scope(changes: Sequence[FileChange], plan: ImplementationPlan) -> CheckRecord:
    """Every change must be a file the approved plan named, with a compatible operation. Test
    files the plan listed under `tests` may be created or modified."""
    allowed = {pc.path: pc.operation for pc in plan.changes}
    tests = set(plan.tests)
    bad = []
    for c in changes:
        op = allowed.get(c.path)
        if op is None and c.path in tests and c.op is not ChangeOp.DELETE:
            continue
        if op is None or (op == "delete") != (c.op is ChangeOp.DELETE):
            bad.append(c.path)
    return CheckRecord(
        kind=CheckKind.PLAN_SCOPE,
        status=F if bad else P,
        detail=f"outside the approved plan: {_list(bad)}" if bad else None,
    )


def change_limits(changes: Sequence[FileChange], profile: RepositoryProfile) -> CheckRecord:
    size = sum(len(c.content or b"") for c in changes)
    lim = profile.limits
    if len(changes) > lim.max_files_changed:
        return CheckRecord(kind=CheckKind.CHANGE_LIMITS, status=F, detail="too_many_files")
    if size > lim.max_diff_bytes:
        return CheckRecord(kind=CheckKind.CHANGE_LIMITS, status=F, detail="diff_too_large")
    return CheckRecord(kind=CheckKind.CHANGE_LIMITS, status=P)
