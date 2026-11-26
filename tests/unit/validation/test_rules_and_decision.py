"""Independent validation: rules, decision and the engine's refusal paths."""

import uuid
from pathlib import Path

from ai_code_change_agent.application.workspaces.models import ChangeOp, FileChange
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.validation import rules
from ai_code_change_agent.validation.decision import commands_check, decide, required_tests
from ai_code_change_agent.validation.engine import ValidationEngine, ValidationInput
from ai_code_change_agent.validation.git_inspector import changed_paths, changeset, changeset_sha256
from ai_code_change_agent.validation.schemas import (
    ChangesetDocument,
    CheckKind,
    CheckStatus,
    CommandRecord,
    Decision,
)
from ai_code_change_agent.validation.security_scanner import secrets
from tests.support.changes import python_profile
from tests.support.workflow import FILES, PLAN
from tests.unit.execution.fakes import ListBaseline, make_runtime

PROFILE = RepositoryProfile.model_validate(python_profile())
GOOD = (FileChange("app/greeting.py", ChangeOp.MODIFY, b"def greet():\n    return 'Bye'\n"),)


def cmd(cid: str, kind: str, status: str) -> CommandRecord:
    return CommandRecord(command_id=cid, kind=kind, status=status, exit_code=0, duration_ms=1)


def test_static_rules() -> None:
    assert rules.changeset_present(()).status is CheckStatus.FAILED
    assert (
        rules.path_safety((FileChange("../x", ChangeOp.CREATE, b""),)).status is CheckStatus.FAILED
    )
    assert (
        rules.protected_paths((FileChange(".github/workflows/x.yml", ChangeOp.CREATE, b""),)).status
        is CheckStatus.FAILED
    )
    assert rules.plan_scope(GOOD, PLAN).status is CheckStatus.PASSED
    outside = (FileChange("app/other.py", ChangeOp.CREATE, b""),)
    assert rules.plan_scope(outside, PLAN).status is CheckStatus.FAILED
    wrong_op = (FileChange("app/greeting.py", ChangeOp.DELETE),)
    assert rules.plan_scope(wrong_op, PLAN).status is CheckStatus.FAILED
    planned_test = (FileChange("tests/test_greeting.py", ChangeOp.CREATE, b"def test(): pass\n"),)
    assert rules.plan_scope(planned_test, PLAN).status is CheckStatus.PASSED
    many = tuple(FileChange(f"f{i}.py", ChangeOp.CREATE, b"") for i in range(60))
    assert rules.change_limits(many, PROFILE).detail == "too_many_files"
    huge = (FileChange("a.py", ChangeOp.CREATE, b"x" * 2_000_000),)
    assert rules.change_limits(huge, PROFILE).detail == "diff_too_large"


def test_secret_detection_fails_the_changeset() -> None:
    leak = (
        FileChange(
            "app/conf.py",
            ChangeOp.CREATE,
            b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n",
        ),
    )
    assert secrets(leak).status is CheckStatus.FAILED
    assert secrets(GOOD).status is CheckStatus.PASSED


def test_required_tests_and_commands_decide_eligibility() -> None:
    assert required_tests([]).detail == "profile_defines_no_test_command"
    assert required_tests([cmd("test", "test", "FAILED")]).status is CheckStatus.FAILED
    assert required_tests([cmd("test", "test", "PASSED")]).status is CheckStatus.PASSED
    blocked = commands_check([cmd("lint", "lint", "BLOCKED")], "strict")
    assert blocked.status is CheckStatus.FAILED and "strict" in str(blocked.detail)
    decision, reasons = decide([blocked])
    assert decision is Decision.FAILED and reasons


def test_changeset_extraction_and_document_round_trip() -> None:
    from ai_code_change_agent.application.workspaces.models import TreeEntry, TreeManifest
    from tests.support.workflow import sha

    base = TreeManifest((TreeEntry("a", 1, sha(b"a")), TreeEntry("b", 1, sha(b"b"))))
    work = TreeManifest((TreeEntry("a", 2, sha(b"aa")), TreeEntry("c", 1, sha(b"c"))))
    ops = changed_paths(base, work)
    changes = changeset(ops, {"a": b"aa", "c": b"c"})
    assert [(c.path, c.op.value) for c in changes] == [
        ("a", "modify"),
        ("b", "delete"),
        ("c", "create"),
    ]
    doc = ChangesetDocument.of("c" * 40, changes)
    assert doc.changes() == changes
    assert changeset_sha256(changes) == changeset_sha256(tuple(reversed(changes)))


def _input(
    changes: tuple[FileChange, ...], profile: RepositoryProfile = PROFILE
) -> ValidationInput:
    async def baseline() -> ListBaseline:
        return ListBaseline(dict(FILES))

    return ValidationInput(
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        "c" * 40,
        profile,
        PLAN,
        changes,
        baseline,
    )


async def test_the_engine_passes_a_clean_change_in_a_fresh_workspace(tmp_path: Path) -> None:
    runtime, _runner = make_runtime(tmp_path)
    report = await ValidationEngine(runtime).validate(_input(GOOD))
    assert report.passed and {c.command_id for c in report.commands} == {"test", "lint"}
    assert any(c.kind is CheckKind.ACCEPTANCE_EVIDENCE and not c.mandatory for c in report.checks)
    assert list((tmp_path / "ws").glob("*/ws_*")) == []  # the fresh workspace was destroyed


async def test_the_engine_never_runs_commands_for_a_statically_bad_change(tmp_path: Path) -> None:
    runtime, runner = make_runtime(tmp_path)
    bad = (FileChange(".env", ChangeOp.CREATE, b"TOKEN=x\n"),)
    report = await ValidationEngine(runtime).validate(_input(bad))
    assert not report.passed and runner.runs() == []
    assert {c.kind for c in report.checks if c.status is CheckStatus.SKIPPED} >= {
        CheckKind.COMMANDS,
        CheckKind.REQUIRED_TESTS,
    }
    assert "protected_paths" in report.feedback()


async def test_a_failing_test_command_fails_validation_with_its_output(tmp_path: Path) -> None:
    runtime, runner = make_runtime(tmp_path)
    runner.on_run = lambda argv: (
        ProcessResult(1, b"1 failed: assert 'Bye'", b"", False, False)
        if "pytest" in argv
        else ProcessResult(0, b"ok", b"", False, False)
    )
    report = await ValidationEngine(runtime).validate(_input(GOOD))
    assert not report.passed and "1 failed" in report.feedback()


async def test_a_change_that_cannot_be_applied_fails_rebuild_integrity(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    missing = (
        FileChange("app/greeting.py", ChangeOp.DELETE),
        FileChange("app/greeting.py", ChangeOp.DELETE),
    )
    plan_ok = PLAN.model_copy(
        update={"changes": [PLAN.changes[0].model_copy(update={"operation": "delete"})]}
    )
    data = _input(missing)
    report = await ValidationEngine(runtime).validate(
        ValidationInput(
            data.request_id,
            data.attempt_id,
            data.repository_id,
            data.profile_version_id,
            data.base_commit_sha,
            PROFILE,
            plan_ok,
            missing,
            data.baseline,
        )
    )
    rebuild = next(c for c in report.checks if c.kind is CheckKind.REBUILD_INTEGRITY)
    assert rebuild.status is CheckStatus.FAILED and not report.passed
