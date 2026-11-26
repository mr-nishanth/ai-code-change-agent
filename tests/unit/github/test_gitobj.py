"""Gate D.1: locally computed Git object ids equal what git itself produces."""

import os
import subprocess
from pathlib import Path

from ai_code_change_agent.integrations.github.gitobj import (
    blob_sha,
    commit_sha,
    tree_sha,
)


def git(
    repo: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None
) -> str:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}  # fmt: skip
    out = subprocess.run(  # noqa: S603
        ["git", f"--git-dir={repo}", *args],  # noqa: S607
        input=data,
        capture_output=True,
        check=True,
        env={**base, **(env or {})},
    )
    return out.stdout.decode().strip()


def test_blob_tree_and_commit_ids_match_git(tmp_path: Path) -> None:
    repo = tmp_path / "r.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)  # noqa: S603, S607
    files = {
        "README.md": (b"hello\n", "100644"),
        "src/app.py": (b"print(1)\n", "100644"),
        "src/app/__init__.py": (b"", "100644"),  # "app" dir sorts after "app.py"
        "bin/run": (b"#!/bin/sh\n", "100755"),
        "a-b/c": (b"x", "100644"),
    }
    entries: dict[str, tuple[str, str]] = {}
    index = "\n".join(
        f"{mode} {git(repo, 'hash-object', '-w', '--stdin', data=data)}\t{path}"
        for path, (data, mode) in files.items()
    )
    for path, (data, mode) in files.items():
        entries[path] = (mode, blob_sha(data))
        assert blob_sha(data) == git(repo, "hash-object", "--stdin", data=data)
    env = {"GIT_INDEX_FILE": str(tmp_path / "index")}
    git(repo, "update-index", "--index-info", data=(index + "\n").encode(), env=env)
    tree = git(repo, "write-tree", env=env)
    assert tree_sha(entries) == tree
    message = "AI Code Change: SHOP-1 (attempt 1)\n\nManifest-SHA256: abc\n"
    who = {"GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x.invalid", "GIT_AUTHOR_DATE": "@1767225600 +0000",
           "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x.invalid",
           "GIT_COMMITTER_DATE": "@1767225600 +0000"}  # fmt: skip
    parent = git(repo, "commit-tree", tree, data=b"seed\n", env=who)
    made = git(repo, "commit-tree", tree, "-p", parent, data=message.encode(), env=who)
    assert commit_sha(
        tree=tree, parent=parent, name="A", email="a@x.invalid", timestamp=1767225600,
        message=message,
    ) == made  # fmt: skip
