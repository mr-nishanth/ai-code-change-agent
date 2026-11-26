"""A scripted docker client for unit tests: records argv, never starts anything."""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ai_code_change_agent.application.workspaces.models import (
    BaselineFile,
    WorkspaceHandle,
    WorkspaceSpec,
)
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.execution.docker.runtime import (
    ContainerLimits,
    DockerWorkspaceRuntime,
)

IMAGE = "ai-code-change-workspace-python:test"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_run: Callable[[list[str]], ProcessResult | BaseException] = lambda argv: (
            ProcessResult(0, b"ok", b"", False, False)
        )
        self.containers: list[str] = []  # ids returned by `docker ps`

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        cli = list(argv)
        self.calls.append(cli)
        verb = cli[1] if len(cli) > 1 else ""
        if verb == "ps":
            return ProcessResult(0, "\n".join(self.containers).encode(), b"", False, False)
        if verb in {"rm", "image", "version"}:
            return ProcessResult(0, b"1.2.3", b"", False, False)
        outcome = self.on_run(cli)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def runs(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == "run"]

    def removals(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == "rm"]


class ListBaseline:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def files(self):  # type: ignore[no-untyped-def]
        for path, data in self._files.items():
            yield BaselineFile(path, data)


def spec(attempt: uuid.UUID | None = None) -> WorkspaceSpec:
    return WorkspaceSpec(
        uuid.uuid4(), attempt or uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "c" * 40
    )


def make_runtime(
    tmp_path: Path, runner: FakeRunner | None = None, **kw: object
) -> tuple[DockerWorkspaceRuntime, FakeRunner]:
    fake = runner or FakeRunner()
    runtime = DockerWorkspaceRuntime(
        root=tmp_path / "ws",
        images={"python": IMAGE},
        policy=CommandPolicy(),
        runner=fake,
        clock=lambda: NOW,
        uid=1000,
        gid=1000,
        **kw,  # type: ignore[arg-type]
    )
    return runtime, fake


FILES = {"app/greeting.py": b"def greet(n):\n    return n\n", "README.md": b"# hi\n"}


async def created(
    tmp_path: Path, files: dict[str, bytes] | None = None, limits: ContainerLimits | None = None
) -> tuple[DockerWorkspaceRuntime, FakeRunner, WorkspaceHandle]:
    runtime, fake = make_runtime(tmp_path, limits=limits)
    handle = await runtime.create(spec(), ListBaseline(files or FILES))
    return runtime, fake, handle


_ = asyncio
