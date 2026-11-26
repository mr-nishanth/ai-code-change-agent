"""Sandbox hardening as code (no Docker needed)."""

import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces.errors import (
    UnsafePathError,
    WorkspaceRuntimeError,
)
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.execution.docker.sandbox import (
    DockerSandbox,
    SandboxLimits,
    SandboxManager,
    sandbox_run_argv,
)
from tests.unit.execution.fakes import FakeRunner, created

NAME = "aica-sbx-12345678-0123456789ab"


async def test_the_container_argv_is_fixed_and_hardened(tmp_path: Path) -> None:
    _, _, handle = await created(tmp_path)
    for writable in (True, False):
        argv = sandbox_run_argv(
            docker="docker",
            name=NAME,
            image="img:1",
            work=tmp_path,
            handle=handle,
            writable=writable,
            limits=SandboxLimits(),
            uid=501,
            gid=20,
        )
        text = " ".join(argv)
        for flag in (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--pull never",
            "--user 501:20",
        ):
            assert flag in text
        for banned in ("--privileged", "docker.sock", "unconfined", "--cap-add", "-v ", "host"):
            assert banned not in text
        assert text.endswith("img:1 sleep 7200")
        assert ("readonly" in text) is (not writable)
        assert not [a for a in argv if a.startswith("--env") and "TOKEN" in a]


async def test_identity_and_root_are_refused(tmp_path: Path) -> None:
    _, _, handle = await created(tmp_path)
    kw = {
        "docker": "docker",
        "work": tmp_path,
        "handle": handle,
        "writable": True,
        "limits": SandboxLimits(),
    }
    with pytest.raises(WorkspaceRuntimeError):
        sandbox_run_argv(name="evil; rm", image="img", uid=1, gid=1, **kw)  # type: ignore[arg-type]
    with pytest.raises(WorkspaceRuntimeError):
        sandbox_run_argv(name=NAME, image="img; x", uid=1, gid=1, **kw)  # type: ignore[arg-type]
    with pytest.raises(WorkspaceRuntimeError):
        sandbox_run_argv(name=NAME, image="img", uid=0, gid=0, **kw)  # type: ignore[arg-type]
    with pytest.raises(WorkspaceRuntimeError):
        DockerSandbox(name="x", work=tmp_path, writable=True, limits=SandboxLimits(), uid=1, gid=1)


def test_only_workspace_paths_are_accepted() -> None:
    assert DockerSandbox._relative("/workspace/app/a.py") == "app/a.py"
    assert DockerSandbox._relative("app/a.py") == "app/a.py"
    for bad in ("/etc/passwd", "/workspace/../etc", "/workspacex/a", "../a"):
        with pytest.raises(UnsafePathError):
            DockerSandbox._relative(bad)


def test_a_read_only_sandbox_refuses_uploads_and_reads_safely(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"x")
    sb = DockerSandbox(
        name=NAME, work=tmp_path, writable=False, limits=SandboxLimits(), uid=1, gid=1
    )
    assert sb.upload_files([("/workspace/b.txt", b"y")])[0].error == "permission_denied"
    got = sb.download_files(
        ["/workspace/a.txt", "/workspace/none.txt", "/etc/passwd", "/workspace"]
    )
    assert got[0].content == b"x"
    assert [g.error for g in got[1:]] == ["file_not_found", "invalid_path", "invalid_path"]
    assert sb.id == NAME and not sb.writable


def test_a_writable_sandbox_writes_only_inside_the_workspace(tmp_path: Path) -> None:
    sb = DockerSandbox(
        name=NAME,
        work=tmp_path,
        writable=True,
        limits=SandboxLimits(max_file_bytes=10),
        uid=1,
        gid=1,
    )
    res = sb.upload_files(
        [
            ("/workspace/a/b.txt", b"hi"),
            ("/workspace/a/b.txt", b"again"),
            ("/etc/x", b"x"),
            ("/workspace/big", b"x" * 11),
        ]
    )
    assert [r.error for r in res] == [None, None, "invalid_path", "permission_denied"]
    assert (tmp_path / "a" / "b.txt").read_bytes() == b"again"


async def test_the_manager_cleans_up_a_failed_start(tmp_path: Path) -> None:
    _, _, handle = await created(tmp_path)
    runner = FakeRunner()
    runner.on_run = lambda argv: ProcessResult(125, b"", b"no such image", False, False)
    manager = SandboxManager(image="img:1", limits=SandboxLimits(), runner=runner, uid=1, gid=1)
    with pytest.raises(WorkspaceRuntimeError, match="sandbox_start_failed"):
        await manager.start(handle, tmp_path, writable=True)
    assert runner.removals()
    runner.on_run = lambda argv: ProcessResult(0, b"id", b"", False, False)
    sb = await manager.start(handle, tmp_path, writable=False)
    await manager.stop(sb)
    assert runner.removals()[-1][-1] == sb.id
    await manager.stop_named("not-a-sandbox")  # ignored
    _ = uuid
