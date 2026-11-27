"""The Docker runtime with a scripted docker client: argv hardening, lifecycle, failure paths."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces import errors as e
from ai_code_change_agent.application.workspaces.models import (
    ApprovedCommand,
    ChangeLimits,
    ChangeOp,
    CommandStatus,
    FileChange,
    WorkspaceHandle,
    WorkspaceStatus,
)
from ai_code_change_agent.domain.repositories.command_policy import CommandKind
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.execution.docker.runtime import (
    ContainerLimits,
    DockerWorkspaceRuntime,
)
from tests.unit.execution.fakes import (
    FILES,
    IMAGE,
    FakeRunner,
    ListBaseline,
    created,
    make_runtime,
    spec,
)

LIMITS = ChangeLimits()
PYTEST = ApprovedCommand(
    "tests", "python", CommandKind.TEST, ("python", "-m", "pytest", "-q", "tests"), ".", 60
)


def change(
    path: str, op: ChangeOp = ChangeOp.MODIFY, content: bytes | None = b"new\n"
) -> FileChange:
    return FileChange(path, op, None if op is ChangeOp.DELETE else content)


# ---- creation and baseline
async def test_create_records_identity_and_a_stable_baseline_hash(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    s = spec()
    handle = await runtime.create(s, ListBaseline(FILES))
    assert handle.id.startswith("ws_") and handle.status is WorkspaceStatus.ACTIVE
    assert (handle.request_id, handle.attempt_id, handle.base_commit_sha) == (
        s.request_id, s.attempt_id, s.base_commit_sha,
    )  # fmt: skip
    base = await runtime.manifest(handle, "baseline")
    assert base.tree_hash == handle.baseline_tree_hash
    assert (await runtime.manifest(handle, "work")).tree_hash == handle.baseline_tree_hash
    (tmp_path / "other").mkdir()
    other, _ = make_runtime(tmp_path / "other")
    again = await other.create(spec(), ListBaseline(dict(reversed(list(FILES.items())))))
    assert again.baseline_tree_hash == handle.baseline_tree_hash  # order independent
    meta = json.loads((tmp_path / "ws" / str(s.attempt_id) / handle.id / "meta.json").read_text())
    assert meta["baseline_tree_hash"] == handle.baseline_tree_hash and meta["id"] == handle.id


async def test_the_handle_carries_no_host_path(tmp_path: Path) -> None:
    _runtime, _, handle = await created(tmp_path)
    assert str(tmp_path) not in repr(handle)


async def test_one_workspace_per_attempt(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    s = spec()
    await runtime.create(s, ListBaseline(FILES))
    with pytest.raises(e.WorkspaceExistsError):
        await runtime.create(s, ListBaseline(FILES))


async def test_a_failed_create_leaves_nothing_behind(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, limits=ContainerLimits(max_files=1))
    s = spec()
    with pytest.raises(e.WorkspaceLimitError):
        await runtime.create(s, ListBaseline(FILES))
    assert list((tmp_path / "ws" / str(s.attempt_id)).iterdir()) == []  # no workspace left
    with pytest.raises(e.UnsafePathError):
        await runtime.create(spec(), ListBaseline({"../escape.txt": b"x"}))


async def test_workspaces_are_isolated_per_attempt_and_handles_cannot_be_forged(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path)
    a = await runtime.create(spec(), ListBaseline({"a.txt": b"A"}))
    b = await runtime.create(spec(), ListBaseline({"b.txt": b"B"}))
    assert await runtime.read_file(a, "a.txt") == b"A"
    with pytest.raises(e.UnsafePathError):
        await runtime.read_file(a, "../" + str(b.attempt_id) + "/work/b.txt")
    forged = WorkspaceHandle(**{**a.__dict__, "id": "ws_" + "0" * 32})
    with pytest.raises(e.WorkspaceNotFoundError):
        await runtime.read_file(forged, "a.txt")
    crossed = WorkspaceHandle(**{**b.__dict__, "attempt_id": a.attempt_id})
    with pytest.raises(e.WorkspaceNotFoundError):
        await runtime.read_file(crossed, "a.txt")


# ---- changes
async def test_changes_are_applied_to_work_and_never_to_the_baseline(tmp_path: Path) -> None:
    runtime, _, h = await created(tmp_path)
    await runtime.apply_changes(
        h,
        [
            change("app/greeting.py"),
            change("app/new.py", ChangeOp.CREATE, b"x = 1\n"),
            change("README.md", ChangeOp.DELETE),
        ],
        LIMITS,
    )
    work, base = await runtime.manifest(h, "work"), await runtime.manifest(h, "baseline")
    assert work.paths == {"app/greeting.py", "app/new.py"} and base.paths == set(FILES)
    assert base.tree_hash == h.baseline_tree_hash != work.tree_hash
    assert await runtime.read_file(h, "app/greeting.py") == b"new\n"
    assert (
        await runtime.read_file(h, "app/greeting.py", tree="baseline") == FILES["app/greeting.py"]
    )


async def test_reset_work_restores_the_baseline(tmp_path: Path) -> None:
    runtime, _, h = await created(tmp_path)
    await runtime.apply_changes(h, [change("app/greeting.py")], LIMITS)
    await runtime.reset_work(h)
    assert (await runtime.manifest(h, "work")).tree_hash == h.baseline_tree_hash


@pytest.mark.parametrize(
    ("bad", "error"),
    [
        (change("../escape.txt", ChangeOp.CREATE), e.UnsafePathError),
        (change("/abs.txt", ChangeOp.CREATE), e.UnsafePathError),
        (change(".github/workflows/ci.yml", ChangeOp.CREATE), e.ProtectedPathError),
        (change(".env", ChangeOp.CREATE), e.ProtectedPathError),
        (change("Dockerfile", ChangeOp.CREATE), e.ProtectedPathError),
        (change("app/greeting.py", ChangeOp.CREATE), e.InvalidChangeError),  # exists
        (change("app/none.py", ChangeOp.MODIFY), e.InvalidChangeError),  # missing
        (change("app/none.py", ChangeOp.DELETE), e.InvalidChangeError),
        (change("bin.dat", ChangeOp.CREATE, b"a\x00b"), e.InvalidChangeError),  # binary
        (FileChange("app/x.py", ChangeOp.CREATE, None), e.InvalidChangeError),
        (FileChange("README.md", ChangeOp.DELETE, b"x"), e.InvalidChangeError),
        (change("big.txt", ChangeOp.CREATE, b"x" * 300_000), e.WorkspaceLimitError),
    ],
)
async def test_invalid_changes_are_refused_and_nothing_is_applied(
    tmp_path: Path, bad: FileChange, error: type[Exception]
) -> None:
    runtime, _, h = await created(tmp_path)
    with pytest.raises(error):
        await runtime.apply_changes(h, [change("README.md"), bad], LIMITS)  # first change is valid
    assert (await runtime.manifest(h, "work")).tree_hash == h.baseline_tree_hash  # all or nothing


async def test_change_set_rules(tmp_path: Path) -> None:
    runtime, _, h = await created(tmp_path)
    for changes in (
        [],
        [change("README.md"), change("README.md")],
        [change("app/greeting.py")] * 51,
    ):
        with pytest.raises(e.InvalidChangeError):
            await runtime.apply_changes(h, changes, ChangeLimits(max_files=50))
    with pytest.raises(e.WorkspaceLimitError):
        await runtime.apply_changes(
            h, [change("README.md", content=b"x" * 20)], ChangeLimits(max_total_bytes=10)
        )


async def test_a_symlink_planted_in_the_work_tree_is_never_followed(tmp_path: Path) -> None:
    runtime, _, h = await created(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    work = tmp_path / "ws" / str(h.attempt_id) / h.id / "work"
    (work / "app").rename(work / "app_real")
    (work / "app").symlink_to(tmp_path)
    with pytest.raises(e.WorkspaceError):  # refused (the path is not a regular file we can see)
        await runtime.apply_changes(h, [change("app/greeting.py")], LIMITS)
    with pytest.raises(e.UnsafePathError):
        await runtime.read_file(h, "app/greeting.py")
    assert outside.read_text() == "keep"


# ---- the container command line
async def test_the_docker_command_line_is_fully_hardened(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    result = await runtime.run_command(h, PYTEST)
    assert result.status is CommandStatus.PASSED and result.stdout == b"ok"
    (cli,) = fake.runs()
    pairs = {cli[i]: cli[i + 1] for i in range(len(cli) - 1) if cli[i].startswith("--")}
    assert pairs["--network"] == "none"
    assert "--read-only" in cli and pairs["--cap-drop"] == "ALL"
    assert pairs["--security-opt"] == "no-new-privileges" and pairs["--pull"] == "never"
    assert pairs["--user"] == "1000:1000" and not cli[cli.index("--user") + 1].startswith("0")
    assert (pairs["--memory"], pairs["--memory-swap"], pairs["--cpus"], pairs["--pids-limit"]) == (
        "512m",
        "512m",
        "1.0",
        "128",
    )
    assert pairs["--tmpfs"].startswith("/tmp:rw,noexec,nosuid")  # noqa: S108 - container path
    assert pairs["--workdir"] == "/workspace"
    # the only mount is this attempt's work tree
    mounts = [cli[i + 1] for i, a in enumerate(cli) if a in {"--mount", "-v", "--volume"}]
    assert mounts == [
        f"type=bind,src={tmp_path / 'ws' / str(h.attempt_id) / h.id / 'work'},dst=/workspace"
    ]
    flat = " ".join(cli)
    for token in (
        "--privileged",
        "--pid",
        "--ipc",
        "--device",
        "--add-host",
        "--cap-add",
        "--env-file",
        "--uts",
        "--userns",
    ):
        assert token not in cli  # exact tokens: "--pids-limit" is allowed and is not "--pid"
    for fragment in ("docker.sock", "--network host", "seccomp=unconfined", "/Users", ".env"):
        assert fragment not in flat
    assert cli[cli.index(IMAGE) :] == [IMAGE, "python", "-m", "pytest", "-q", "tests"]
    assert "aica.managed=true" in cli and f"aica.workspace={h.id}" in cli


async def test_only_fixed_and_policy_allowed_environment_reaches_the_container(
    tmp_path: Path,
) -> None:
    runtime, fake, h = await created(tmp_path)
    os.environ["AWS_SECRET_ACCESS_KEY"] = "do-not-leak"
    try:
        cmd = ApprovedCommand(
            "tests", "python", CommandKind.TEST, ("pytest", "-q", "tests"), "src", 30, {"TZ": "UTC"}
        )
        await runtime.run_command(h, cmd)
    finally:
        del os.environ["AWS_SECRET_ACCESS_KEY"]
    (cli,) = fake.runs()
    env = {cli[i + 1].split("=")[0] for i, a in enumerate(cli) if a == "--env"}
    assert env == {
        "HOME", "PYTHONDONTWRITEBYTECODE", "MYPY_CACHE_DIR", "RUFF_CACHE_DIR", "PYTEST_ADDOPTS", "TZ",
    }  # fmt: skip
    assert "do-not-leak" not in " ".join(cli)
    assert cli[cli.index("--workdir") + 1] == "/workspace/src"


@pytest.mark.parametrize(
    "command",
    [
        ApprovedCommand("c", "python", CommandKind.TEST, ("bash", "-c", "id"), ".", 30),
        ApprovedCommand("c", "python", CommandKind.TEST, ("python", "-c", "import os"), ".", 30),
        ApprovedCommand("c", "python", CommandKind.TEST, ("pytest", "-q; id"), ".", 30),
        ApprovedCommand("c", "python", CommandKind.TEST, ("pytest",), "../x", 30),
        ApprovedCommand(
            "c", "python", CommandKind.TEST, ("pytest",), ".", 30, {"LD_PRELOAD": "/x"}
        ),
        ApprovedCommand(
            "c", "python", CommandKind.INSTALL, ("pip", "install", "x"), ".", 30, {}, True
        ),
        ApprovedCommand("c", "python", CommandKind.TEST, ("pytest",), ".", 30, {}, True),  # network
        ApprovedCommand("c", "rust-unknown", CommandKind.TEST, ("pytest",), ".", 30),
        ApprovedCommand(
            "c", "node", CommandKind.TEST, ("npm", "test"), ".", 30
        ),  # no image for node
    ],
)
async def test_the_runtime_rechecks_policy_and_blocks_before_starting_anything(
    tmp_path: Path, command: ApprovedCommand
) -> None:
    runtime, fake, h = await created(tmp_path)
    result = await runtime.run_command(h, command)
    assert result.status is CommandStatus.BLOCKED and result.reason
    assert fake.runs() == []


async def test_exit_codes_map_to_statuses_and_output_truncation_is_reported(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    cases = [
        (ProcessResult(1, b"fail", b"", False, False), CommandStatus.FAILED, None),
        (ProcessResult(5, b"", b"", False, False), CommandStatus.FAILED, None),
        (ProcessResult(125, b"", b"", False, False), CommandStatus.ERROR, "container_start_failed"),
        (ProcessResult(127, b"", b"", False, False), CommandStatus.ERROR, "container_start_failed"),
        (
            ProcessResult(None, b"partial", b"", False, True),
            CommandStatus.TIMED_OUT,
            "command_timeout",
        ),
    ]
    for outcome, status, reason in cases:
        fake.on_run = lambda argv, o=outcome: o  # type: ignore[misc]
        result = await runtime.run_command(h, PYTEST)
        assert (result.status, result.reason) == (status, reason)
    fake.on_run = lambda argv: ProcessResult(0, b"x" * 10, b"", True, False)
    assert (await runtime.run_command(h, PYTEST)).output_truncated


@pytest.mark.parametrize(
    "exit_code", [137, 139, 143, None], ids=["sigkill_oom", "sigsegv", "sigterm", "no_status"]
)
async def test_a_container_that_dies_unexpectedly_is_a_failed_command_never_a_pass(
    tmp_path: Path, exit_code: int | None
) -> None:
    runtime, fake, h = await created(tmp_path)
    fake.on_run = lambda argv: ProcessResult(exit_code, b"partial", b"", False, False)
    result = await runtime.run_command(h, PYTEST)
    assert result.status is CommandStatus.FAILED and result.exit_code == exit_code
    name = fake.runs()[0][fake.runs()[0].index("--name") + 1]
    assert ["docker", "rm", "-f", name] in fake.removals()  # nothing is left running


async def test_a_timeout_removes_the_container_by_name(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    fake.on_run = lambda argv: ProcessResult(None, b"", b"", False, True)
    await runtime.run_command(h, PYTEST)
    name = fake.runs()[0][fake.runs()[0].index("--name") + 1]
    assert ["docker", "rm", "-f", name] in fake.removals()


async def test_cancellation_removes_the_container_and_propagates(tmp_path: Path) -> None:
    started = asyncio.Event()

    class Hanging(FakeRunner):
        async def run(self, argv, *, deadline, max_output):  # type: ignore[no-untyped-def]
            if len(argv) > 1 and argv[1] == "run":
                self.calls.append(list(argv))
                started.set()
                await asyncio.Event().wait()  # a container that never finishes
            return await super().run(argv, deadline=deadline, max_output=max_output)

    runner = Hanging()
    runtime, _ = make_runtime(tmp_path, runner)
    handle = await runtime.create(spec(), ListBaseline(FILES))
    task = asyncio.create_task(runtime.run_command(handle, PYTEST))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    name = runner.runs()[0][runner.runs()[0].index("--name") + 1]
    assert ["docker", "rm", "-f", name] in runner.removals()  # removed by name, then re-raised


async def test_docker_being_unavailable_is_a_runtime_error_not_a_pass(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    fake.on_run = lambda argv: FileNotFoundError("docker")
    with pytest.raises(e.WorkspaceRuntimeError, match="docker_unavailable"):
        await runtime.run_command(h, PYTEST)


async def test_a_command_that_fills_the_disk_is_reported(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path, limits=ContainerLimits(max_workspace_bytes=1024))
    work = tmp_path / "ws" / str(h.attempt_id) / h.id / "work"

    def fill(argv: list[str]) -> ProcessResult:
        (work / "huge.bin").write_bytes(b"0" * 5000)
        return ProcessResult(0, b"", b"", False, False)

    fake.on_run = fill
    result = await runtime.run_command(h, PYTEST)
    assert (result.status, result.reason) == (CommandStatus.ERROR, "workspace_size_exceeded")


# ---- cleanup
async def test_destroy_removes_containers_and_files_and_is_idempotent(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    fake.containers = ["abcdef123456", "not-an-id!"]
    result = await runtime.destroy(h)
    assert (result.directory_removed, result.containers_removed, result.runtime_available) == (
        True,
        1,
        True,
    )
    assert result.clean and list((tmp_path / "ws" / str(h.attempt_id)).iterdir()) == []
    assert ["docker", "rm", "-f", "abcdef123456"] in fake.removals()
    assert (await runtime.destroy(h)).directory_removed
    with pytest.raises(e.WorkspaceNotFoundError):
        await runtime.manifest(h)


async def test_destroy_still_removes_files_when_docker_is_gone(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    fake.on_run = lambda argv: FileNotFoundError("docker")

    async def broken(argv, *, deadline, max_output):  # type: ignore[no-untyped-def]
        raise FileNotFoundError

    fake.run = broken  # type: ignore[method-assign]
    result = await runtime.destroy(h)
    assert result.directory_removed and not result.runtime_available and not result.clean


async def test_a_symlink_in_place_of_the_workspace_directory_is_unlinked_not_followed(
    tmp_path: Path,
) -> None:
    runtime, _, h = await created(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    ws = tmp_path / "ws" / str(h.attempt_id) / h.id
    import shutil

    shutil.rmtree(ws)
    ws.symlink_to(victim)
    await runtime.destroy(h)
    assert (victim / "keep.txt").read_text() == "keep" and not ws.exists()


async def test_sweep_removes_only_expired_workspaces(tmp_path: Path) -> None:
    runtime, _fake, old = await created(tmp_path)
    meta_path = tmp_path / "ws" / str(old.attempt_id) / old.id / "meta.json"
    meta_path.chmod(0o600)
    doc = json.loads(meta_path.read_text())
    doc["created_at"] = "2026-11-25T08:00:00+00:00"  # four hours before the test clock
    meta_path.write_text(json.dumps(doc))
    fresh = await runtime.create(spec(), ListBaseline(FILES))
    (tmp_path / "ws" / "not-a-workspace").mkdir()
    assert await runtime.sweep(3600) == 1
    assert list((tmp_path / "ws" / str(old.attempt_id)).iterdir()) == []
    assert (tmp_path / "ws" / str(fresh.attempt_id) / fresh.id).exists()


async def test_check_ready_reports_docker_and_images(tmp_path: Path) -> None:
    runtime, fake, _ = await created(tmp_path)
    assert await runtime.check_ready() == {"docker": True, "image:python": True}
    fake.run = FakeRunner().run  # type: ignore[method-assign]
    assert "image:python" in await runtime.check_ready()


# ---- construction
def test_the_runtime_refuses_to_run_as_root_or_with_unsafe_configuration(tmp_path: Path) -> None:
    from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy

    common = {"images": {"python": IMAGE}, "policy": CommandPolicy()}
    with pytest.raises(e.WorkspaceRuntimeError, match="root"):
        DockerWorkspaceRuntime(root=tmp_path / "a", uid=0, **common)  # type: ignore[arg-type]
    with pytest.raises(e.WorkspaceRuntimeError, match="image_name_invalid"):
        DockerWorkspaceRuntime(
            root=tmp_path / "b",
            uid=1000,
            images={"python": "bad image; rm"},
            policy=CommandPolicy(),
        )
    with pytest.raises(e.WorkspaceRuntimeError, match="not_absolute"):
        DockerWorkspaceRuntime(root=Path("relative"), uid=1000, **common)  # type: ignore[arg-type]
    bad_chars = tmp_path / "with space,comma"
    bad_chars.mkdir(mode=0o700)
    with pytest.raises(e.WorkspaceRuntimeError, match="characters_unsupported"):
        DockerWorkspaceRuntime(root=bad_chars, uid=1000, **common)  # type: ignore[arg-type]


async def test_an_image_name_chosen_at_call_time_is_validated(tmp_path: Path) -> None:
    runtime, fake, h = await created(tmp_path)
    with pytest.raises(e.WorkspaceRuntimeError, match="image_name_invalid"):
        await runtime._execute(
            h, command_id="x", image="evil image", argv=["id"], cwd=".", deadline_seconds=5, env={}
        )
    assert fake.runs() == []


_ = uuid


async def test_a_new_workspace_for_the_same_attempt_never_reuses_a_path(tmp_path: Path) -> None:
    runtime, fake = make_runtime(tmp_path)
    s = spec()
    first = await runtime.create(s, ListBaseline(FILES))
    await runtime.destroy(first)
    parent = tmp_path / "ws" / str(s.attempt_id)
    inode = parent.stat().st_ino
    second = await runtime.create(s, ListBaseline(FILES))
    assert second.id != first.id and parent.stat().st_ino == inode  # parent kept, child renamed
    await runtime.run_command(second, PYTEST)
    assert str(tmp_path / "ws" / str(s.attempt_id) / second.id / "work") in " ".join(fake.runs()[0])


async def test_sweep_also_removes_stale_empty_attempt_directories(tmp_path: Path) -> None:
    import os
    import time

    runtime, _ = make_runtime(tmp_path)
    handle = await runtime.create(spec(), ListBaseline(FILES))
    await runtime.destroy(handle)
    parent = tmp_path / "ws" / str(handle.attempt_id)
    os.utime(parent, (time.time() - 99_999, time.time() - 99_999))
    assert await runtime.sweep(3600) == 0 and not parent.exists()
