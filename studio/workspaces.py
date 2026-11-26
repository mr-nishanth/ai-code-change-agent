"""One isolated workspace per studio thread, seeded from a local directory (dev only).

The agent runs in the project's hardened DockerSandbox (no network, read-only root, non-root, no
secrets). The registry lives in this server process: restarting `langgraph dev` starts threads
on a fresh copy of the seed."""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_code_change_agent.application.workspaces.models import (
    BaselineFile,
    WorkspaceHandle,
    WorkspaceSpec,
)
from ai_code_change_agent.composition import build_sandbox_manager
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.execution.docker.runtime import DockerWorkspaceRuntime
from ai_code_change_agent.execution.docker.sandbox import DockerSandbox, SandboxManager

_log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = Path(os.environ.get("STUDIO_SEED_DIR", ROOT / "tests/fixtures/sample_service")).resolve()
WORKSPACE_ROOT = (ROOT / "var/studio/workspaces").resolve()
SKIP_PARTS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
MAX_SEED_BYTES = 5 * 1024 * 1024


class SeedBaseline:
    """Files of the seed directory; a trailing `.fixture` suffix is dropped (test fixtures)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def files(self) -> AsyncIterator[BaselineFile]:
        total = 0
        for path in sorted(self._root.rglob("*")):
            rel = path.relative_to(self._root)
            if path.is_symlink() or not path.is_file() or SKIP_PARTS & set(rel.parts):
                continue
            data = path.read_bytes()
            total += len(data)
            if total > MAX_SEED_BYTES:
                raise ValueError("seed_directory_too_large")
            name = rel.as_posix().removesuffix(".fixture")
            yield BaselineFile(name, data)


@dataclass
class StudioWorkspace:
    thread_id: str
    handle: WorkspaceHandle

    @property
    def work(self) -> Path:
        return _runtime().tree_path(self.handle, "work")

    @property
    def baseline(self) -> Path:
        return _runtime().tree_path(self.handle, "baseline")


_settings: Settings | None = None
_rt: DockerWorkspaceRuntime | None = None
_manager: SandboxManager | None = None
_workspaces: dict[str, StudioWorkspace] = {}
_lock = asyncio.Lock()


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _runtime() -> DockerWorkspaceRuntime:
    global _rt
    if _rt is None:
        s = settings()
        if s.workspace_image is None:
            raise RuntimeError("WORKSPACE_IMAGE is required")
        _rt = DockerWorkspaceRuntime(
            root=WORKSPACE_ROOT, images={"python": s.workspace_image}, policy=CommandPolicy()
        )
    return _rt


def sandboxes() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = build_sandbox_manager(settings())
    return _manager


def _thread_uuid(thread_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(thread_id)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"studio:{thread_id}")


def _recover(thread_id: str) -> StudioWorkspace | None:
    """`langgraph dev` persists threads to disk; find the thread's workspace after a restart."""
    attempt_dir = WORKSPACE_ROOT / str(_thread_uuid(thread_id))
    if not attempt_dir.is_dir() or attempt_dir.is_symlink():
        return None
    for base in sorted(attempt_dir.glob("ws_*"), reverse=True):
        try:
            meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
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
            _runtime().tree_path(handle, "work")  # validates the handle against meta.json
        except Exception:  # an unusable entry is skipped, not fatal
            _log.warning("studio_workspace_unrecoverable", extra={"entry": base.name})
            continue
        return StudioWorkspace(thread_id, handle)
    return None


async def workspace_for(thread_id: str, *, create: bool = True) -> StudioWorkspace | None:
    async with _lock:
        ws = _workspaces.get(thread_id) or _recover(thread_id)
        if ws is not None:
            _workspaces[thread_id] = ws
            return ws
        if not create:
            return None
        attempt = _thread_uuid(thread_id)  # one stable workspace directory per thread
        spec = WorkspaceSpec(attempt, attempt, uuid.uuid4(), uuid.uuid4(), "0" * 40)
        handle = await _runtime().create(spec, SeedBaseline(SEED_DIR))
        ws = StudioWorkspace(thread_id, handle)
        _workspaces[thread_id] = ws
        return ws


def placeholder_sandbox(writable: bool) -> DockerSandbox:
    """For schema/state introspection only: never started, never executed."""
    s = settings()
    from ai_code_change_agent.execution.docker.sandbox import SandboxLimits

    return DockerSandbox(
        name="aica-sbx-00000000-000000000000",
        work=WORKSPACE_ROOT,
        writable=writable,
        limits=SandboxLimits(
            memory_mb=s.workspace_memory_limit_mb,
            pids=s.workspace_pids_limit,
            command_timeout_seconds=s.deep_agent_execute_timeout_seconds,
            disk_bytes=s.workspace_disk_limit_mb * 1024 * 1024,
        ),
        uid=os.getuid(),
        gid=os.getgid(),
    )


async def destroy_all() -> None:
    for ws in list(_workspaces.values()):
        await _runtime().destroy(ws.handle)
    _workspaces.clear()
