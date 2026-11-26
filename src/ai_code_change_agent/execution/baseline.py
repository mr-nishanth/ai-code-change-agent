"""Baseline sources. `DirectoryBaseline` reads a local directory tree (fixtures, exports)."""

import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

from ai_code_change_agent.application.workspaces.errors import (
    UnsupportedEntryError,
    WorkspaceLimitError,
)
from ai_code_change_agent.application.workspaces.models import BaselineFile
from ai_code_change_agent.execution.fs import read_file


class DirectoryBaseline:
    """Streams the regular files of a directory. `.git` is skipped; symlinks and special files are
    refused (fail closed) rather than followed or copied."""

    def __init__(
        self, root: Path, *, max_files: int = 5_000, max_file_bytes: int = 1_048_576
    ) -> None:
        self._root = root
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes

    async def files(self) -> AsyncIterator[BaselineFile]:
        count = 0
        for current, dirs, names in os.walk(self._root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            for name in sorted([*dirs, *names]):
                info = os.lstat(os.path.join(current, name))
                if stat.S_ISLNK(info.st_mode) or not (
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                ):
                    raise UnsupportedEntryError
            for name in sorted(names):
                full = Path(current) / name
                count += 1
                if count > self._max_files:
                    raise WorkspaceLimitError
                rel = full.relative_to(self._root).as_posix()
                data = read_file(self._root, rel, self._max_file_bytes)
                yield BaselineFile(rel, data, bool(os.lstat(full).st_mode & stat.S_IXUSR))
