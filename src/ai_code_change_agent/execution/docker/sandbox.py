"""The Deep Agent's execution sandbox: one hardened, long-lived container per phase.

The Deep Agents SDK requires a `SandboxBackendProtocol` for its `execute` tool; `BaseSandbox`
implements every file tool on top of `execute`, `upload_files` and `download_files`. This
adapter provides them over `docker exec` into a container the application started and owns:

- no network (`--network none`), non-root user, read-only root filesystem, all capabilities
  dropped, `no-new-privileges`, Docker's default seccomp profile (never `unconfined`), CPU,
  memory, PID, open-file and output limits, a size-limited `/tmp` tmpfs;
- the only writable path is the attempt's workspace copy, bind-mounted at `/workspace`
  (mounted read-only for the planning phase);
- no Docker socket, no host paths, no environment from the application (credentials never
  enter the container), no image pulls;
- every command runs under `timeout -s KILL` inside the container, so a killed client never
  leaves a process running; workspace disk use is checked after every command.

Filesystem permissions are not the security boundary: the container is.
"""

import contextlib
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from ai_code_change_agent.application.workspaces import errors as e
from ai_code_change_agent.application.workspaces.models import WorkspaceHandle
from ai_code_change_agent.application.workspaces.paths import validate_relative_path
from ai_code_change_agent.execution import fs
from ai_code_change_agent.execution.docker.dependency_mount import dependency_args
from ai_code_change_agent.execution.docker.process import ProcessRunner

MOUNT_POINT = "/workspace"
# Flat file names inside the container's own /tmp tmpfs (never a host path).
_TMP = re.compile(r"/tmp/[A-Za-z0-9._-]{1,128}")  # noqa: S108
_NAME = re.compile(r"aica-sbx-[0-9a-f]{8}-[0-9a-f]{12}")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?")
FIXED_ENV = {
    "HOME": "/tmp",  # noqa: S108 - a path inside the container
    "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_INDEX": "1",  # no network anyway; make the intent explicit to package tools
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
    "MYPY_CACHE_DIR": "/tmp/.mypy_cache",  # noqa: S108
    "RUFF_CACHE_DIR": "/tmp/.ruff_cache",  # noqa: S108
}


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = 1.0
    memory_mb: int = 2048
    pids: int = 256
    nofile: int = 1024
    tmpfs_mb: int = 256
    disk_bytes: int = 1024 * 1024 * 1024
    command_timeout_seconds: int = 300
    lifetime_seconds: int = 7200
    max_output_bytes: int = 100_000
    max_file_bytes: int = 5 * 1024 * 1024


def sandbox_run_argv(
    *,
    docker: str,
    name: str,
    image: str,
    work: Path,
    handle: WorkspaceHandle,
    writable: bool,
    limits: SandboxLimits,
    uid: int,
    gid: int,
) -> list[str]:
    """The only way a sandbox container is started. Fixed hardening; nothing comes from a
    repository, a Story or a model."""
    if not _NAME.fullmatch(name) or not _IMAGE.fullmatch(image):
        raise e.WorkspaceRuntimeError("sandbox_identity_invalid")
    if uid == 0:
        raise e.WorkspaceRuntimeError("refusing_to_run_as_root")
    mount = f"type=bind,src={work},dst={MOUNT_POINT}" + ("" if writable else ",readonly")
    argv = [
        docker,
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        "aica.managed=true",
        "--label",
        f"aica.attempt={handle.attempt_id}",
        "--label",
        f"aica.workspace={handle.id}",
        "--label",
        "aica.role=agent-sandbox",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(limits.pids),
        "--memory",
        f"{limits.memory_mb}m",
        "--memory-swap",
        f"{limits.memory_mb}m",
        "--cpus",
        str(limits.cpus),
        "--ulimit",
        f"nofile={limits.nofile}:{limits.nofile}",
        "--user",
        f"{uid}:{gid}",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_mb}m",  # noqa: S108
        "--mount",
        mount,
        *dependency_args(handle.dependency_dir),
        "--workdir",
        MOUNT_POINT,
    ]
    for key, value in FIXED_ENV.items():
        argv += ["--env", f"{key}={value}"]
    return [*argv, image, "sleep", str(limits.lifetime_seconds)]


def _run_capped(argv: list[str], timeout: float, cap: int) -> tuple[int | None, bytes, bool]:
    """Run the docker client, keeping at most `cap` bytes of combined output."""
    env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
    proc = subprocess.Popen(  # noqa: S603 - fixed docker argv
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    assert proc.stdout is not None  # noqa: S101 - PIPE requested
    kept = bytearray()
    truncated = False

    def drain() -> None:
        nonlocal truncated
        assert proc.stdout is not None  # noqa: S101
        while chunk := proc.stdout.read(65_536):
            room = cap - len(kept)
            if room > 0:
                kept.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code: int | None = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        code = None
    reader.join(timeout=5)
    return code, bytes(kept), truncated


class DockerSandbox(BaseSandbox):
    """Deep Agents sandbox backend over one running sandbox container."""

    def __init__(
        self,
        *,
        name: str,
        work: Path,
        writable: bool,
        limits: SandboxLimits,
        uid: int,
        gid: int,
        docker: str = "docker",
    ) -> None:
        if not _NAME.fullmatch(name):
            raise e.WorkspaceRuntimeError("sandbox_identity_invalid")
        self._name = name
        self._work = work
        self._writable = writable
        self._limits = limits
        self._uid, self._gid = uid, gid
        self._docker = docker
        self.disk_limit_exceeded = False

    @property
    def id(self) -> str:
        return self._name

    @property
    def writable(self) -> bool:
        return self._writable

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        limit = self._limits.command_timeout_seconds
        seconds = limit if not timeout or timeout <= 0 else min(timeout, limit)
        argv = [
            self._docker,
            "exec",
            "--workdir",
            MOUNT_POINT,
            "--user",
            f"{self._uid}:{self._gid}",
            self._name,
            "timeout",
            "-s",
            "KILL",
            str(seconds),
            "/bin/sh",
            "-c",
            command,
        ]
        code, out, truncated = _run_capped(argv, seconds + 15, self._limits.max_output_bytes)
        text = out.decode("utf-8", errors="replace")
        if code == 137 or code is None:
            text += f"\n[sandbox] command stopped after {seconds}s or killed"
        if self._writable and fs.tree_size(self._work) > self._limits.disk_bytes:
            self.disk_limit_exceeded = True
            return ExecuteResponse(
                text + "\n[sandbox] workspace disk limit exceeded; the run will be stopped",
                1,
                truncated,
            )
        return ExecuteResponse(text, code, truncated)

    @staticmethod
    def _relative(path: str) -> str:
        """Only paths inside /workspace (absolute) or relative to it are accepted."""
        if path.startswith(MOUNT_POINT + "/"):
            return validate_relative_path(path[len(MOUNT_POINT) + 1 :])
        if path.startswith("/"):
            raise e.UnsafePathError  # anything outside the workspace mount
        return validate_relative_path(path)

    def _write_container_tmp(self, path: str, data: bytes) -> bool:
        """Write into the container's own /tmp tmpfs (the SDK stages large edits there)."""
        argv = [
            self._docker,
            "exec",
            "-i",
            "--user",
            f"{self._uid}:{self._gid}",
            self._name,
            "/bin/sh",
            "-c",
            'cat > "$1"',
            "sh",
            path,
        ]
        env = {
            k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ
        }
        done = subprocess.run(  # noqa: S603 - fixed docker argv; data on stdin
            argv, input=data, capture_output=True, timeout=60, env=env, check=False
        )
        return done.returncode == 0

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse] = []
        for path, data in files:
            if not self._writable:
                results.append(FileUploadResponse(path=path, error="permission_denied"))
                continue
            try:
                if len(data) > self._limits.max_file_bytes:
                    raise e.WorkspaceLimitError
                if _TMP.fullmatch(path):
                    ok = self._write_container_tmp(path, data)
                    results.append(
                        FileUploadResponse(path=path, error=None if ok else "permission_denied")
                    )
                    continue
                rel = self._relative(path)
                exists = fs.exists_regular(self._work, rel)
                fs.write_file(self._work, rel, data, create=not exists, executable=False)
                results.append(FileUploadResponse(path=path, error=None))
            except e.UnsafePathError:
                results.append(FileUploadResponse(path=path, error="invalid_path"))
            except (e.WorkspaceError, OSError):
                results.append(FileUploadResponse(path=path, error="permission_denied"))
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            try:
                rel = self._relative(path)
                if not fs.exists_regular(self._work, rel):
                    results.append(
                        FileDownloadResponse(path=path, content=None, error="file_not_found")
                    )
                    continue
                data = fs.read_file(self._work, rel, self._limits.max_file_bytes)
                results.append(FileDownloadResponse(path=path, content=data, error=None))
            except e.UnsafePathError:
                results.append(FileDownloadResponse(path=path, content=None, error="invalid_path"))
            except FileNotFoundError:
                results.append(
                    FileDownloadResponse(path=path, content=None, error="file_not_found")
                )
            except IsADirectoryError:
                results.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except (e.WorkspaceError, OSError):
                results.append(
                    FileDownloadResponse(path=path, content=None, error="permission_denied")
                )
        return results


class SandboxManager:
    """Starts and stops sandbox containers for workspaces created by DockerWorkspaceRuntime.
    The application owns the lifecycle; the agent can never start, stop or reconfigure one."""

    def __init__(
        self,
        *,
        image: str,
        limits: SandboxLimits,
        runner: ProcessRunner,
        docker: str = "docker",
        uid: int | None = None,
        gid: int | None = None,
        images: Mapping[str, str] | None = None,  # toolchain family -> execution image
    ) -> None:
        self._image = image
        self._images = dict(images or {})
        self._limits = limits
        self._runner = runner
        self._docker = docker
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid

    async def start(
        self, handle: WorkspaceHandle, work: Path, *, writable: bool, toolchain: str = "python"
    ) -> DockerSandbox:
        name = f"aica-sbx-{str(handle.attempt_id)[:8]}-{uuid.uuid4().hex[:12]}"
        argv = sandbox_run_argv(
            docker=self._docker,
            name=name,
            image=self._images.get(toolchain, self._image),
            work=work,
            handle=handle,
            writable=writable,
            limits=self._limits,
            uid=self._uid,
            gid=self._gid,
        )
        try:
            result = await self._runner.run(argv, deadline=60, max_output=65_536)
        except OSError as exc:
            raise e.WorkspaceRuntimeError("docker_unavailable") from exc
        if result.exit_code != 0:
            await self.stop_named(name)
            raise e.WorkspaceRuntimeError("sandbox_start_failed")
        return DockerSandbox(
            name=name,
            work=work,
            writable=writable,
            limits=self._limits,
            uid=self._uid,
            gid=self._gid,
            docker=self._docker,
        )

    async def stop(self, sandbox: DockerSandbox) -> None:
        await self.stop_named(sandbox.id)

    async def stop_named(self, name: str) -> None:
        if _NAME.fullmatch(name):
            with contextlib.suppress(OSError):
                await self._runner.run(
                    [self._docker, "rm", "-f", name], deadline=30, max_output=65_536
                )
