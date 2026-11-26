"""Workspace ports. The Docker adapter is the only code that starts containers."""

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol

from ai_code_change_agent.application.workspaces.models import (
    ApprovedCommand,
    BaselineFile,
    ChangeLimits,
    CleanupResult,
    CommandResult,
    FileChange,
    TreeManifest,
    WorkspaceHandle,
    WorkspaceSpec,
)

Tree = Literal["baseline", "work"]


class BaselineSource(Protocol):
    """Streams the files of one exact commit. Implementations enforce their own bounds."""

    def files(self) -> AsyncIterator[BaselineFile]: ...


class BaselineProvider(Protocol):
    """Where an attempt's baseline files come from (a fixture today, the Git provider in Gate D).

    The provider is chosen by the composition root from the registry, never by a Story or a model.
    """

    async def baseline_for(
        self, *, repository_id: uuid.UUID, profile_version_id: uuid.UUID, base_commit_sha: str
    ) -> BaselineSource: ...


class WorkspaceRuntime(Protocol):
    async def create(self, spec: WorkspaceSpec, baseline: BaselineSource) -> WorkspaceHandle: ...

    async def manifest(self, handle: WorkspaceHandle, tree: Tree = "work") -> TreeManifest: ...

    async def read_file(
        self, handle: WorkspaceHandle, path: str, *, tree: Tree = "work", max_bytes: int = 262_144
    ) -> bytes: ...

    async def apply_changes(
        self, handle: WorkspaceHandle, changes: Sequence[FileChange], limits: ChangeLimits
    ) -> None:
        """Validate every change first, then apply them. Nothing is applied if one is invalid."""
        ...

    async def reset_work(self, handle: WorkspaceHandle) -> None:
        """Rebuild the writable tree from the untouched baseline."""
        ...

    async def run_command(
        self, handle: WorkspaceHandle, command: ApprovedCommand
    ) -> CommandResult: ...

    async def destroy(self, handle: WorkspaceHandle) -> CleanupResult:
        """Always safe to call again; removes containers and files."""
        ...
