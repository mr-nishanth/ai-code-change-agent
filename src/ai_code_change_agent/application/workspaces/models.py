"""Workspace value objects."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ai_code_change_agent.application.workspaces.paths import tree_hash
from ai_code_change_agent.domain.repositories.command_policy import CommandKind


class WorkspaceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DESTROYED = "DESTROYED"


@dataclass(frozen=True)
class WorkspaceSpec:
    request_id: uuid.UUID
    attempt_id: uuid.UUID
    repository_id: uuid.UUID
    profile_version_id: uuid.UUID
    base_commit_sha: str
    dependency_dir: str | None = None  # a verified, read-only dependency bundle (ADR-010)


@dataclass(frozen=True)
class TreeEntry:
    path: str
    size: int
    sha256: str
    executable: bool = False


@dataclass(frozen=True)
class TreeManifest:
    entries: tuple[TreeEntry, ...]

    @property
    def tree_hash(self) -> str:
        return tree_hash((e.path, e.executable, e.sha256) for e in self.entries)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(e.path for e in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries)

    def get(self, path: str) -> TreeEntry | None:
        return next((e for e in self.entries if e.path == path), None)


@dataclass(frozen=True)
class WorkspaceHandle:
    """An opaque reference. It carries no host path; the runtime derives locations itself."""

    id: str
    request_id: uuid.UUID
    attempt_id: uuid.UUID
    repository_id: uuid.UUID
    profile_version_id: uuid.UUID
    base_commit_sha: str
    baseline_tree_hash: str
    created_at: datetime
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    dependency_dir: str | None = None


@dataclass(frozen=True)
class BaselineFile:
    path: str
    content: bytes
    executable: bool = False


class ChangeOp(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


@dataclass(frozen=True)
class FileChange:
    path: str
    op: ChangeOp
    content: bytes | None = None


@dataclass(frozen=True)
class ChangeLimits:
    max_files: int = 50
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class ApprovedCommand:
    """A command taken from a published profile version and checked against the platform policy.

    Build it only with `authorise_command`. The runtime checks it again before running it.
    """

    command_id: str
    family: str
    kind: CommandKind
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    env: Mapping[str, str] = field(default_factory=dict)
    network_requested: bool = False


class CommandStatus(StrEnum):
    PASSED = "PASSED"  # exit code 0
    FAILED = "FAILED"  # the command ran and returned non-zero
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"  # refused before running (policy, network)
    ERROR = "ERROR"  # could not run (runtime failure, command not found)


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    status: CommandStatus
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    output_truncated: bool
    duration_ms: int
    reason: str | None = None  # fixed code when the command did not run to completion


@dataclass(frozen=True)
class CleanupResult:
    directory_removed: bool
    containers_removed: int
    runtime_available: bool

    @property
    def clean(self) -> bool:
        return self.directory_removed and self.runtime_available
