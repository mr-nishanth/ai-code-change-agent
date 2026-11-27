"""Validation report: the engine's decision and the evidence behind it."""

import base64
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ai_code_change_agent.application.workspaces.models import ChangeOp, FileChange


class CheckKind(StrEnum):
    CHANGESET_PRESENT = "changeset_present"
    PATH_SAFETY = "path_safety"
    PROTECTED_PATHS = "protected_paths"
    PLAN_SCOPE = "plan_scope"
    CHANGE_LIMITS = "change_limits"
    SECRETS = "secrets"
    REBUILD_INTEGRITY = "rebuild_integrity"
    REQUIRED_TESTS = "required_tests"
    COMMANDS = "commands"
    ACCEPTANCE_EVIDENCE = "acceptance_evidence"  # informational


class CheckStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INFO = "INFO"


class Decision(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class CheckRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CheckKind
    status: CheckStatus
    detail: str | None = Field(default=None, max_length=500)
    mandatory: bool = True


class CommandRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    kind: str
    status: str  # PASSED / FAILED / TIMED_OUT / BLOCKED / ERROR
    exit_code: int | None
    duration_ms: int
    output_tail: str = Field(default="", max_length=8000)
    # Fixed runtime code when the command did not run to completion (container_start_failed,
    # command_not_found, command_timeout...). Absent from reports stored before it existed.
    reason: str | None = Field(default=None, max_length=64)


class ChangedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    operation: str
    size: int
    sha256: str | None


class ChangesetFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    operation: ChangeOp
    content_b64: str | None = None


class ChangesetDocument(BaseModel):
    """What the implementation phase changed, as extracted by the platform (never by the agent)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_commit_sha: str
    files: list[ChangesetFile]

    @classmethod
    def of(cls, base_commit_sha: str, changes: tuple[FileChange, ...]) -> "ChangesetDocument":
        return cls(
            base_commit_sha=base_commit_sha,
            files=[
                ChangesetFile(
                    path=c.path,
                    operation=c.op,
                    content_b64=None
                    if c.content is None
                    else base64.b64encode(c.content).decode("ascii"),
                )
                for c in changes
            ],
        )

    def changes(self) -> tuple[FileChange, ...]:
        return tuple(
            FileChange(
                f.path,
                f.operation,
                None if f.content_b64 is None else base64.b64decode(f.content_b64),
            )
            for f in self.files
        )


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    base_commit_sha: str
    validation_policy: str
    changeset_sha256: str
    changed_files: list[ChangedFile]
    checks: list[CheckRecord]
    commands: list[CommandRecord]
    reasons: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.decision is Decision.PASSED

    def feedback(self) -> str:
        """Text for a bounded repair attempt: failed checks and command output tails only."""
        lines = [
            f"{c.kind.value}: {c.detail or c.status.value}"
            for c in self.checks
            if c.status is CheckStatus.FAILED
        ]
        for cmd in self.commands:
            if cmd.status != "PASSED":
                lines.append(f"command {cmd.command_id} ({cmd.status}):\n{cmd.output_tail[-3000:]}")
        return "\n".join(lines)
