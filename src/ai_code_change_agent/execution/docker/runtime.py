"""Docker implementation of `WorkspaceRuntime`.

The host platform edits files (see `fs.py`); the container only runs approved commands against a
bind-mounted copy. Every container is started with the same fixed hardening; none of it is
configurable per request, per repository, or by a model.
"""

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_code_change_agent.application.artifacts.errors import ArtifactStorageError
from ai_code_change_agent.application.workspaces import errors as e
from ai_code_change_agent.application.workspaces.models import (
    ApprovedCommand,
    BaselineFile,
    ChangeLimits,
    ChangeOp,
    CleanupResult,
    CommandResult,
    CommandStatus,
    FileChange,
    TreeManifest,
    WorkspaceHandle,
    WorkspaceSpec,
    WorkspaceStatus,
)
from ai_code_change_agent.application.workspaces.paths import (
    is_protected,
    validate_relative_path,
)
from ai_code_change_agent.application.workspaces.ports import BaselineSource, Tree
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.execution import fs
from ai_code_change_agent.execution.artifacts import ensure_private_root
from ai_code_change_agent.execution.docker.dependency_mount import dependency_args
from ai_code_change_agent.execution.docker.process import (
    ProcessRunner,
    SubprocessRunner,
)

_log = get_logger("workspace")
_WORKSPACE_ID = re.compile(r"ws_[0-9a-f]{32}")
_SAFE_MOUNT_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,100})?(@sha256:[0-9a-f]{64})?|sha256:[0-9a-f]{64}"
)
_DOCKER_FAILURE_CODES = frozenset({125, 126, 127})  # docker itself, or "cannot execute"
# Fixed by the platform, never by a repository or a model. Tool caches go to the container's
# tmpfs so they neither count against the workspace quota nor appear as changed files.
_CONTAINER_TMP = "/tmp"  # noqa: S108 - a path inside the container
_FIXED_ENV = {
    "HOME": _CONTAINER_TMP,
    "PYTHONDONTWRITEBYTECODE": "1",
    "MYPY_CACHE_DIR": f"{_CONTAINER_TMP}/.mypy_cache",
    "RUFF_CACHE_DIR": f"{_CONTAINER_TMP}/.ruff_cache",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ContainerLimits:
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    tmpfs_mb: int = 64
    max_output_bytes: int = 262_144
    max_workspace_bytes: int = 50 * 1024 * 1024
    max_files: int = 5_000
    max_file_bytes: int = 1_048_576
    nofile: int = 1024


class DockerWorkspaceRuntime:
    def __init__(
        self,
        *,
        root: Path,
        images: Mapping[str, str],
        policy: CommandPolicy,
        limits: ContainerLimits | None = None,
        runner: ProcessRunner | None = None,
        docker_binary: str = "docker",
        uid: int | None = None,
        gid: int | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        dependency_root: Path | None = None,
    ) -> None:
        self._dependency_root = dependency_root.resolve() if dependency_root else None
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        if self._uid == 0:
            raise e.WorkspaceRuntimeError("refusing_to_run_as_root")
        for image in images.values():
            if not _IMAGE.fullmatch(image):
                raise e.WorkspaceRuntimeError("image_name_invalid")
        try:
            self._root = ensure_private_root(root)
        except ArtifactStorageError as exc:
            raise e.WorkspaceRuntimeError(str(exc)) from exc
        if not _SAFE_MOUNT_PATH.fullmatch(str(self._root)):
            raise e.WorkspaceRuntimeError("workspace_root_characters_unsupported")
        self._images = dict(images)
        self._policy = policy
        self._limits = limits or ContainerLimits()
        self._runner = runner or SubprocessRunner()
        self._docker = docker_binary
        self._clock = clock

    # ----- locations (derived from UUIDs only) --------------------------------------------
    def _attempt_dir(self, attempt_id: uuid.UUID) -> Path:
        return self._root / str(uuid.UUID(str(attempt_id)))

    def _dir(self, handle: WorkspaceHandle) -> Path:
        """<root>/<attempt-uuid>/<workspace-id>. The attempt directory is a stable parent that is
        never deleted and recreated, and every workspace gets a new name beneath it: Docker
        Desktop's file sharing can keep a stale entry for a path that was deleted and recreated,
        after which the daemon refuses to bind-mount it ("bind source path does not exist")."""
        if not _WORKSPACE_ID.fullmatch(handle.id):
            raise e.WorkspaceNotFoundError
        return self._attempt_dir(handle.attempt_id) / handle.id

    def tree_path(self, handle: WorkspaceHandle, tree: Tree = "work") -> Path:
        """Host path of a workspace tree (validated handle); used to mount it into a sandbox."""
        return self._tree(handle, tree)

    def _tree(self, handle: WorkspaceHandle, tree: Tree) -> Path:
        base = self._dir(handle)
        if handle.status is not WorkspaceStatus.ACTIVE or not base.is_dir() or base.is_symlink():
            raise e.WorkspaceNotFoundError
        meta = self._read_meta(base)
        if meta.get("id") != handle.id:
            raise e.WorkspaceNotFoundError  # a stale or forged handle
        return base / tree

    @staticmethod
    def _read_meta(base: Path) -> dict[str, Any]:
        try:
            return dict(json.loads(fs.read_file(base, "meta.json", 65_536)))
        except (OSError, ValueError, e.WorkspaceError) as exc:
            raise e.WorkspaceNotFoundError from exc

    # ----- lifecycle ----------------------------------------------------------------------
    def _dependency_dir(self, value: str | None) -> str | None:
        """Only a bundle directory under the configured dependency root may be mounted."""
        if value is None:
            return None
        path = Path(value).resolve()
        root = self._dependency_root
        if (
            root is None
            or not path.is_relative_to(root)
            or not path.is_dir()
            or not _SAFE_MOUNT_PATH.fullmatch(str(path))
        ):
            raise e.WorkspaceRuntimeError("dependency_dir_not_allowed")
        return str(path)

    async def create(self, spec: WorkspaceSpec, baseline: BaselineSource) -> WorkspaceHandle:
        dependency_dir = self._dependency_dir(spec.dependency_dir)
        attempt_dir = self._attempt_dir(spec.attempt_id)
        if attempt_dir.is_symlink():
            raise e.WorkspaceExistsError
        attempt_dir.mkdir(mode=0o700, exist_ok=True)
        if any(attempt_dir.iterdir()):  # one workspace per attempt at a time
            raise e.WorkspaceExistsError
        workspace_id = f"ws_{uuid.uuid4().hex}"
        base = attempt_dir / workspace_id
        base.mkdir(mode=0o700)
        try:
            (base / "baseline").mkdir(mode=0o700)
            (base / "work").mkdir(mode=0o700)
            count = total = 0
            async for item in baseline.files():
                await self._copy_in(base, item)
                count += 1
                total += len(item.content)
                if count > self._limits.max_files or total > self._limits.max_workspace_bytes:
                    raise e.WorkspaceLimitError
            manifest = await asyncio.to_thread(
                fs.manifest,
                base / "baseline",
                max_entries=self._limits.max_files,
                max_bytes=self._limits.max_workspace_bytes,
            )
            handle = WorkspaceHandle(
                id=workspace_id,
                request_id=spec.request_id,
                attempt_id=spec.attempt_id,
                repository_id=spec.repository_id,
                profile_version_id=spec.profile_version_id,
                base_commit_sha=spec.base_commit_sha,
                baseline_tree_hash=manifest.tree_hash,
                created_at=self._clock(),
                dependency_dir=dependency_dir,
            )
            meta = {
                "id": handle.id,
                "request_id": str(spec.request_id),
                "attempt_id": str(spec.attempt_id),
                "repository_id": str(spec.repository_id),
                "profile_version_id": str(spec.profile_version_id),
                "base_commit_sha": spec.base_commit_sha,
                "baseline_tree_hash": handle.baseline_tree_hash,
                "created_at": handle.created_at.isoformat(),
                "status": handle.status.value,
                "dependency_dir": dependency_dir,
            }
            await asyncio.to_thread(
                fs.write_file,
                base,
                "meta.json",
                json.dumps(meta, sort_keys=True).encode(),
                create=True,
            )
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, base, True)
            raise
        _log.info(
            "workspace_created",
            workspace_id=handle.id,
            attempt_id=str(spec.attempt_id),
            files=count,
            bytes=total,
            baseline_tree_hash=handle.baseline_tree_hash,
        )
        return handle

    async def _copy_in(self, base: Path, item: BaselineFile) -> None:
        for tree in ("baseline", "work"):
            await asyncio.to_thread(
                fs.write_file,
                base / tree,
                item.path,
                item.content,
                create=True,
                executable=item.executable,
            )

    async def destroy(self, handle: WorkspaceHandle) -> CleanupResult:
        base = self._dir(handle)
        removed, available = await self._remove_containers(handle.id)
        gone = True
        if base.exists() or base.is_symlink():
            if base.is_symlink():
                base.unlink()  # never follow
            else:
                await asyncio.to_thread(shutil.rmtree, base, True)
            gone = not base.exists()
        result = CleanupResult(gone, removed, available)
        _log.info(
            "workspace_destroyed",
            workspace_id=handle.id,
            directory_removed=gone,
            containers_removed=removed,
            runtime_available=available,
        )
        return result

    async def destroy_attempt_workspaces(self, attempt_id: uuid.UUID) -> int:
        """Crash recovery: remove the workspaces (and containers) a previous run of the SAME
        attempt left behind. Only one runner holds an attempt at a time (job lease), so these
        are stale by construction. Returns the number destroyed."""
        attempt_dir = self._attempt_dir(attempt_id)
        if not attempt_dir.is_dir() or attempt_dir.is_symlink():
            return 0
        destroyed = 0
        for entry in sorted(attempt_dir.iterdir()):
            try:
                meta = self._read_meta(entry)
                handle = WorkspaceHandle(
                    id=str(meta["id"]),
                    request_id=uuid.UUID(meta["request_id"]),
                    attempt_id=uuid.UUID(meta["attempt_id"]),
                    repository_id=uuid.UUID(meta["repository_id"]),
                    profile_version_id=uuid.UUID(meta["profile_version_id"]),
                    base_commit_sha=str(meta["base_commit_sha"]),
                    baseline_tree_hash=str(meta["baseline_tree_hash"]),
                    created_at=datetime.fromisoformat(str(meta["created_at"])),
                )
            except (e.WorkspaceError, KeyError, ValueError):
                continue
            if handle.attempt_id != attempt_id:
                continue
            await self.destroy(handle)
            destroyed += 1
        if destroyed:
            _log.info("stale_workspaces_destroyed", attempt_id=str(attempt_id), count=destroyed)
        return destroyed

    async def sweep(self, older_than_seconds: int) -> int:
        """Destroy workspaces (and their containers) older than the limit. Returns the count."""
        swept = 0
        entries = [
            ws
            for attempt in sorted(self._root.iterdir())
            if attempt.is_dir() and not attempt.is_symlink()
            for ws in sorted(attempt.iterdir())
        ]
        for entry in entries:
            try:
                meta = self._read_meta(entry)
                created = datetime.fromisoformat(str(meta["created_at"]))
            except (e.WorkspaceError, KeyError, ValueError):
                continue
            if (self._clock() - created).total_seconds() < older_than_seconds:
                continue
            handle = WorkspaceHandle(
                id=str(meta["id"]),
                request_id=uuid.UUID(meta["request_id"]),
                attempt_id=uuid.UUID(meta["attempt_id"]),
                repository_id=uuid.UUID(meta["repository_id"]),
                profile_version_id=uuid.UUID(meta["profile_version_id"]),
                base_commit_sha=str(meta["base_commit_sha"]),
                baseline_tree_hash=str(meta["baseline_tree_hash"]),
                created_at=created,
            )
            await self.destroy(handle)
            swept += 1
        for attempt in sorted(self._root.iterdir()):  # stale empty parents of finished attempts
            if attempt.is_dir() and not attempt.is_symlink() and not any(attempt.iterdir()):
                age = time.time() - attempt.stat().st_mtime  # file times are wall-clock
                if age >= older_than_seconds:
                    attempt.rmdir()
        return swept

    # ----- files ---------------------------------------------------------------------------
    async def manifest(self, handle: WorkspaceHandle, tree: Tree = "work") -> TreeManifest:
        return await asyncio.to_thread(
            fs.manifest,
            self._tree(handle, tree),
            max_entries=self._limits.max_files,
            max_bytes=self._limits.max_workspace_bytes,
        )

    async def read_file(
        self, handle: WorkspaceHandle, path: str, *, tree: Tree = "work", max_bytes: int = 262_144
    ) -> bytes:
        return await asyncio.to_thread(fs.read_file, self._tree(handle, tree), path, max_bytes)

    async def apply_changes(
        self, handle: WorkspaceHandle, changes: Sequence[FileChange], limits: ChangeLimits
    ) -> None:
        work = self._tree(handle, "work")
        await asyncio.to_thread(self._validate_changes, work, changes, limits)
        for change in changes:
            if change.op is ChangeOp.DELETE:
                await asyncio.to_thread(fs.delete_file, work, change.path)
            else:
                assert change.content is not None  # noqa: S101 - validated above
                await asyncio.to_thread(
                    fs.write_file,
                    work,
                    change.path,
                    change.content,
                    create=change.op is ChangeOp.CREATE,
                )

    @staticmethod
    def _validate_changes(work: Path, changes: Sequence[FileChange], limits: ChangeLimits) -> None:
        if not changes or len(changes) > limits.max_files:
            raise e.InvalidChangeError("change_count_out_of_range")
        seen: set[str] = set()
        total = 0
        for change in changes:
            path = validate_relative_path(change.path)
            if path in seen:
                raise e.InvalidChangeError("duplicate_path")
            seen.add(path)
            if is_protected(path):
                raise e.ProtectedPathError
            present = fs.exists_regular(work, path)
            if change.op is ChangeOp.CREATE and present:
                raise e.InvalidChangeError("path_already_exists")
            if change.op in {ChangeOp.MODIFY, ChangeOp.DELETE} and not present:
                raise e.InvalidChangeError("path_missing")
            if change.op is ChangeOp.DELETE:
                if change.content is not None:
                    raise e.InvalidChangeError("delete_has_content")
                continue
            if change.content is None:
                raise e.InvalidChangeError("content_required")
            if len(change.content) > limits.max_file_bytes:
                raise e.WorkspaceLimitError
            if b"\x00" in change.content:
                raise e.InvalidChangeError("binary_content_not_allowed")
            total += len(change.content)
            if total > limits.max_total_bytes:
                raise e.WorkspaceLimitError

    async def reset_work(self, handle: WorkspaceHandle) -> None:
        base = self._dir(handle)
        work = self._tree(handle, "work")
        await asyncio.to_thread(
            _empty_directory, work
        )  # keep the directory itself (same mount path)
        baseline = base / "baseline"
        manifest = await self.manifest(handle, "baseline")
        for entry in manifest.entries:
            data = await asyncio.to_thread(fs.read_file, baseline, entry.path, entry.size + 1)
            await asyncio.to_thread(
                fs.write_file,
                base / "work",
                entry.path,
                data,
                create=True,
                executable=entry.executable,
            )

    # ----- commands ------------------------------------------------------------------------
    async def run_command(self, handle: WorkspaceHandle, command: ApprovedCommand) -> CommandResult:
        """Re-check the policy now; refuse anything that needs the network; then run."""
        findings = self._policy.check(
            field_path=f"commands.{command.command_id}",
            family=command.family,
            kind=command.kind,
            argv=command.argv,
            cwd=command.cwd,
            timeout_seconds=command.timeout_seconds,
            env=command.env,
            network_requested=command.network_requested,
        )
        if findings or command.network_requested:
            return self._blocked(command.command_id, "command_rejected_by_policy")
        image = self._images.get(command.family)
        if image is None:
            return self._blocked(command.command_id, "no_image_for_toolchain")
        return await self._execute(
            handle,
            command_id=command.command_id,
            image=image,
            argv=command.argv,
            cwd=command.cwd,
            deadline_seconds=command.timeout_seconds,
            env=command.env,
        )

    @staticmethod
    def _blocked(command_id: str, reason: str) -> CommandResult:
        return CommandResult(command_id, CommandStatus.BLOCKED, None, b"", b"", False, 0, reason)

    async def _execute(
        self,
        handle: WorkspaceHandle,
        *,
        command_id: str,
        image: str,
        argv: Sequence[str],
        cwd: str,
        deadline_seconds: float,
        env: Mapping[str, str],
    ) -> CommandResult:
        """Run one container. Internal: callers outside this class go through `run_command`
        (only the Docker security tests call this directly, to probe the sandbox itself)."""
        work = self._tree(handle, "work")
        if not _IMAGE.fullmatch(image):
            raise e.WorkspaceRuntimeError("image_name_invalid")
        name = f"aica-{str(handle.attempt_id)[:8]}-{uuid.uuid4().hex[:10]}"
        cli = self._docker_run_argv(handle, name, image, work, argv, cwd, env)
        started = time.perf_counter()
        try:
            result = await self._runner.run(
                cli, deadline=deadline_seconds, max_output=self._limits.max_output_bytes
            )  # on expiry the container is removed by name below
        except asyncio.CancelledError:
            await asyncio.shield(self._remove_named(name))
            raise
        except OSError as exc:
            raise e.WorkspaceRuntimeError("docker_unavailable") from exc
        ms = int((time.perf_counter() - started) * 1000)
        await self._remove_named(name)  # --rm normally did it; this covers every other path

        def finish(status: CommandStatus, reason: str | None = None) -> CommandResult:
            return CommandResult(
                command_id,
                status,
                result.exit_code,
                result.stdout,
                result.stderr,
                result.truncated,
                ms,
                reason,
            )

        if result.timed_out:
            return finish(CommandStatus.TIMED_OUT, "command_timeout")
        if await asyncio.to_thread(fs.tree_size, work) > self._limits.max_workspace_bytes:
            return finish(CommandStatus.ERROR, "workspace_size_exceeded")
        if result.exit_code in _DOCKER_FAILURE_CODES:
            return finish(CommandStatus.ERROR, "container_start_failed")
        status = CommandStatus.PASSED if result.exit_code == 0 else CommandStatus.FAILED
        _log.info(
            "workspace_command",
            workspace_id=handle.id,
            command_id=command_id,
            status=status.value,
            exit_code=result.exit_code,
            duration_ms=ms,
        )
        return finish(status)

    def _docker_run_argv(
        self,
        handle: WorkspaceHandle,
        name: str,
        image: str,
        work: Path,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
    ) -> list[str]:
        lim = self._limits
        workdir = "/workspace" if cwd == "." else f"/workspace/{validate_relative_path(cwd)}"
        if not _SAFE_MOUNT_PATH.fullmatch(str(work)):
            raise e.WorkspaceRuntimeError("workspace_path_characters_unsupported")
        cli = [
            self._docker,
            "run",
            "--rm",
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
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(lim.pids),
            "--memory",
            f"{lim.memory_mb}m",
            "--memory-swap",
            f"{lim.memory_mb}m",
            "--cpus",
            str(lim.cpus),
            "--ulimit",
            f"nofile={lim.nofile}:{lim.nofile}",
            "--user",
            f"{self._uid}:{self._gid}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={lim.tmpfs_mb}m",  # noqa: S108
            "--mount",
            f"type=bind,src={work},dst=/workspace",
            *dependency_args(handle.dependency_dir),
            "--workdir",
            workdir,
        ]
        for key, value in {**_FIXED_ENV, **env}.items():
            cli += ["--env", f"{key}={value}"]
        return [*cli, image, *argv]

    # ----- container housekeeping ---------------------------------------------------------
    async def _docker_call(self, *args: str, deadline: float = 30) -> tuple[int | None, bytes]:
        try:
            res = await self._runner.run(
                [self._docker, *args], deadline=deadline, max_output=65_536
            )
        except OSError:
            return None, b""
        return res.exit_code, res.stdout

    async def _remove_named(self, name: str) -> None:
        await self._docker_call("rm", "-f", name)

    async def _remove_containers(self, workspace_id: str) -> tuple[int, bool]:
        code, out = await self._docker_call(
            "ps", "-aq", "--filter", f"label=aica.workspace={workspace_id}"
        )
        if code != 0:
            return 0, False
        ids = [
            line
            for line in out.decode(errors="replace").split()
            if re.fullmatch(r"[0-9a-f]{6,64}", line)
        ]
        if ids:
            await self._docker_call("rm", "-f", *ids)
        return len(ids), True

    async def check_ready(self) -> dict[str, bool]:
        """Docker reachable, and each configured image present locally (nothing is pulled)."""
        status = {
            "docker": (await self._docker_call("version", "--format", "{{.Server.Version}}"))[0]
            == 0
        }
        for family, image in sorted(self._images.items()):
            status[f"image:{family}"] = (await self._docker_call("image", "inspect", image))[0] == 0
        return status


def _empty_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child, ignore_errors=True)


__all__ = ["ContainerLimits", "DockerWorkspaceRuntime"]
