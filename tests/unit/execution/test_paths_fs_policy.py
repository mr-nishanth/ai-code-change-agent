"""Path rules, safe file operations, baseline reading and command authorisation."""

import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces import errors as e
from ai_code_change_agent.application.workspaces.paths import (
    is_protected,
    tree_hash,
    validate_relative_path,
)
from ai_code_change_agent.application.workspaces.policy import authorise_command
from ai_code_change_agent.domain.repositories.command_policy import CommandKind, CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.execution import fs
from ai_code_change_agent.execution.baseline import DirectoryBaseline


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "/etc/passwd",
        "~/x",
        "a/../b",
        "..",
        "a//b",
        "./a",
        "a/./b",
        "a\\b",
        "C:/x",
        "a\x00b",
        "x" * 301,
    ],
)
def test_unsafe_relative_paths_are_rejected(bad: str) -> None:
    with pytest.raises(e.UnsafePathError):
        validate_relative_path(bad)


def test_unsafe_path_error_is_a_value_error_for_schema_validators() -> None:
    assert issubclass(e.UnsafePathError, ValueError)


@pytest.mark.parametrize(
    "path",
    [".git/config", ".github/workflows/ci.yml", ".env", ".env.local", "app/.secrets", "Dockerfile",
     "docker/Dockerfile.prod", "docker-compose.yml", "compose.yaml", "Jenkinsfile",
     "deploy/id_rsa", "certs/server.pem", "k/private.key", "build/release-ci.yml",
     "bitbucket-pipelines.yml", ".gitmodules", "x/.hidden/y.txt"],
)  # fmt: skip
def test_protected_paths(path: str) -> None:
    assert is_protected(path)


@pytest.mark.parametrize(
    "path",
    ["app/greeting.py", "tests/test_x.py", "README.md", "src/a/b.ts"],
)
def test_ordinary_paths_are_not_protected(path: str) -> None:
    assert not is_protected(path)


def test_tree_hash_is_order_independent_and_content_sensitive() -> None:
    a = [("a.py", False, "1" * 64), ("b.py", False, "2" * 64)]
    assert tree_hash(a) == tree_hash(reversed(a))
    assert tree_hash(a) != tree_hash([("a.py", True, "1" * 64), ("b.py", False, "2" * 64)])
    assert tree_hash(a) != tree_hash([("a.py", False, "1" * 64), ("b.py", False, "3" * 64)])
    assert tree_hash(a) != tree_hash([("a.py", False, "1" * 64)])


# ---- safe file operations
@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir(mode=0o700)
    return root


def test_write_read_modify_delete_round_trip(base: Path) -> None:
    fs.write_file(base, "a/b/c.txt", b"one", create=True)
    assert fs.read_file(base, "a/b/c.txt", 100) == b"one"
    fs.write_file(base, "a/b/c.txt", b"two", create=False)
    assert fs.read_file(base, "a/b/c.txt", 100) == b"two"
    assert fs.exists_regular(base, "a/b/c.txt")
    fs.delete_file(base, "a/b/c.txt")
    assert not fs.exists_regular(base, "a/b/c.txt")


def test_create_never_overwrites_and_modify_needs_an_existing_file(base: Path) -> None:
    fs.write_file(base, "f.txt", b"x", create=True)
    with pytest.raises(e.InvalidChangeError, match="path_already_exists"):
        fs.write_file(base, "f.txt", b"y", create=True)
    with pytest.raises(e.InvalidChangeError):
        fs.write_file(base, "missing.txt", b"y", create=False)
    with pytest.raises(e.InvalidChangeError, match="path_missing"):
        fs.delete_file(base, "missing.txt")


def test_symlink_in_the_middle_of_a_path_is_refused(base: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    (base / "link").symlink_to(outside)
    with pytest.raises(e.UnsafePathError):
        fs.read_file(base, "link/secret.txt", 100)
    with pytest.raises(e.UnsafePathError):
        fs.write_file(base, "link/new.txt", b"x", create=True)
    with pytest.raises(e.UnsafePathError):
        fs.delete_file(base, "link/secret.txt")
    assert not fs.exists_regular(base, "link/secret.txt")
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]  # nothing written outside


def test_symlink_as_the_final_component_is_refused(base: Path, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("t")
    (base / "evil.txt").symlink_to(target)
    with pytest.raises(e.UnsafePathError):
        fs.read_file(base, "evil.txt", 100)
    with pytest.raises(e.InvalidChangeError):
        fs.write_file(base, "evil.txt", b"x", create=False)
    with pytest.raises(e.UnsupportedEntryError):
        fs.delete_file(base, "evil.txt")
    assert target.read_text() == "t"


def test_oversize_read_is_refused(base: Path) -> None:
    fs.write_file(base, "big.txt", b"x" * 50, create=True)
    with pytest.raises(e.WorkspaceLimitError):
        fs.read_file(base, "big.txt", 10)


def test_manifest_hashes_files_and_fails_closed_on_special_entries(
    base: Path, tmp_path: Path
) -> None:
    fs.write_file(base, "b.py", b"2", create=True)
    fs.write_file(base, "a/x.py", b"1", create=True, executable=True)
    manifest = fs.manifest(base, max_entries=10, max_bytes=100)
    assert [(e_.path, e_.executable) for e_ in manifest.entries] == [
        ("a/x.py", True),
        ("b.py", False),
    ]
    assert (
        manifest.total_bytes == 2
        and manifest.get("b.py") is not None
        and manifest.get("zz") is None
    )
    (base / "link").symlink_to(tmp_path)
    with pytest.raises(e.UnsupportedEntryError):
        fs.manifest(base, max_entries=10, max_bytes=100)
    (base / "link").unlink()
    fifo = base / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(e.UnsupportedEntryError):
        fs.manifest(base, max_entries=10, max_bytes=100)
    fifo.unlink()
    short = Path(tempfile.mkdtemp(prefix="s", dir="/tmp"))  # AF_UNIX paths are length limited
    try:
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(short / "s"))
        sock.close()
        with pytest.raises(e.UnsupportedEntryError):
            fs.manifest(short, max_entries=10, max_bytes=100)
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_manifest_limits(base: Path) -> None:
    for i in range(3):
        fs.write_file(base, f"f{i}.txt", b"abcd", create=True)
    with pytest.raises(e.WorkspaceLimitError):
        fs.manifest(base, max_entries=2, max_bytes=100)
    with pytest.raises(e.WorkspaceLimitError):
        fs.manifest(base, max_entries=10, max_bytes=8)
    assert fs.tree_size(base) == 12


async def test_directory_baseline_streams_files_and_skips_git(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "config").write_text("x")
    (src / "a").mkdir()
    (src / "a" / "m.py").write_text("print(1)")
    (src / "run.sh").write_text("#!/bin/sh")
    (src / "run.sh").chmod(0o755)
    seen = {f.path: f.executable async for f in DirectoryBaseline(src).files()}
    assert seen == {"a/m.py": False, "run.sh": True}


async def test_directory_baseline_refuses_symlinks_and_oversize_trees(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.txt").write_text("x")
    (src / "link").symlink_to(tmp_path)
    with pytest.raises(e.UnsupportedEntryError):
        [f async for f in DirectoryBaseline(src).files()]
    (src / "link").unlink()
    with pytest.raises(e.WorkspaceLimitError):
        [f async for f in DirectoryBaseline(src, max_files=0).files()]
    (src / "big.txt").write_bytes(b"x" * 20)
    with pytest.raises(e.WorkspaceLimitError):
        [f async for f in DirectoryBaseline(src, max_file_bytes=10).files()]


# ---- command authorisation
def profile(*commands: dict[str, object]) -> RepositoryProfile:
    return RepositoryProfile.model_validate(
        {
            "schema_version": 1,
            "repository_key": "demo",
            "toolchains": ["python"],
            "command_policy": "default-v1",
            "validation_policy": "standard",
            "commands": list(commands),
        }
    )


def cmd(cid: str, argv: list[str], **kw: object) -> dict[str, object]:
    return {"id": cid, "kind": kw.pop("kind", "test"), "toolchain": "python", "argv": argv, **kw}


def test_a_profile_command_becomes_an_approved_command() -> None:
    p = profile(cmd("tests", ["python", "-m", "pytest", "-q", "tests"], timeout_seconds=120))
    approved = authorise_command(p, CommandPolicy(), "tests")
    assert approved.argv == ("python", "-m", "pytest", "-q", "tests")
    assert (approved.family, approved.kind, approved.timeout_seconds) == (
        "python",
        CommandKind.TEST,
        120,
    )
    assert not approved.network_requested


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (cmd("xx", ["python", "-c", "print(1)"]), "command_rejected_by_policy"),
        (cmd("xx", ["pytest", "tests; rm -rf /"]), "command_rejected_by_policy"),
        (cmd("xx", ["bash", "-c", "id"]), "command_rejected_by_policy"),
        (cmd("xx", ["pytest", "-q", "../outside"]), "command_rejected_by_policy"),
        (cmd("xx", ["pytest"], cwd="../up"), "command_rejected_by_policy"),
        (cmd("xx", ["pytest"], env={"AWS_SECRET_ACCESS_KEY": "x"}), "command_rejected_by_policy"),
        (cmd("xx", ["pip", "install", "left-pad"], kind="install", network="package_registry"), "network_commands_unsupported"),
    ],
)  # fmt: skip
def test_unsafe_profile_commands_are_blocked(command: dict[str, object], reason: str) -> None:
    with pytest.raises(e.CommandBlockedError, match=reason):
        authorise_command(profile(command), CommandPolicy(), "xx")


def test_a_command_id_that_is_not_in_the_profile_is_blocked() -> None:
    with pytest.raises(e.CommandBlockedError, match="command_not_in_profile"):
        authorise_command(profile(cmd("tests", ["pytest"])), CommandPolicy(), "model-invented")


def test_a_fifo_in_a_tree_never_blocks_a_read_or_a_write(base: Path) -> None:
    import threading

    os.mkfifo(base / "pipe")
    outcome: list[str] = []

    def attempt() -> None:
        for call in (
            lambda: fs.read_file(base, "pipe", 10),
            lambda: fs.write_file(base, "pipe", b"x", create=False),
        ):
            try:
                call()
            except e.WorkspaceError:
                outcome.append("refused")

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "a FIFO blocked the host"
    assert outcome == ["refused", "refused"]
