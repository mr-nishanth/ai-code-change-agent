"""Safe file operations inside one directory tree.

Every operation walks the path one component at a time with `openat`-style calls
(`dir_fd`, `O_NOFOLLOW`, `O_DIRECTORY`), so a symlink planted anywhere on the path is refused
instead of followed, and the final component is opened without following links. Regular files only.
"""

import hashlib
import os
import stat
from pathlib import Path

from ai_code_change_agent.application.workspaces.errors import (
    InvalidChangeError,
    UnsafePathError,
    UnsupportedEntryError,
    WorkspaceLimitError,
)
from ai_code_change_agent.application.workspaces.models import TreeEntry, TreeManifest
from ai_code_change_agent.application.workspaces.paths import validate_relative_path

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Never block on a FIFO or device planted in a tree: the open fails or returns at once, and the
# file-type check right after the open rejects anything that is not a regular file.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY


def _open_parent(base: Path, rel: str, *, create: bool) -> tuple[int, str]:
    """Open the parent directory of `rel` under `base`; return (dir fd, final name)."""
    parts = validate_relative_path(rel).split("/")
    fd = os.open(base, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:  # ELOOP / ENOTDIR: a symlink or a file in the way
                raise UnsafePathError from exc
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def read_file(base: Path, rel: str, max_bytes: int) -> bytes:
    dir_fd, name = _open_parent(base, rel, create=False)
    try:
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dir_fd)
        except OSError as exc:
            raise UnsafePathError from exc
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UnsupportedEntryError
            data = handle.read(max_bytes + 1)
    finally:
        os.close(dir_fd)
    if len(data) > max_bytes:
        raise WorkspaceLimitError
    return data


def write_file(
    base: Path, rel: str, data: bytes, *, create: bool, executable: bool = False
) -> None:
    """`create=True` requires the file to be absent; otherwise it must be a regular file."""
    dir_fd, name = _open_parent(base, rel, create=create)
    try:
        flags = (
            os.O_WRONLY | _NOFOLLOW | _NONBLOCK | (os.O_CREAT | os.O_EXCL if create else os.O_TRUNC)
        )
        try:
            fd = os.open(name, flags, 0o700 if executable else 0o600, dir_fd=dir_fd)
        except FileExistsError as exc:
            raise InvalidChangeError("path_already_exists") from exc
        except OSError as exc:
            raise InvalidChangeError("path_not_writable") from exc
        with os.fdopen(fd, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UnsupportedEntryError
            handle.write(data)
    finally:
        os.close(dir_fd)


def delete_file(base: Path, rel: str) -> None:
    dir_fd, name = _open_parent(base, rel, create=False)
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise UnsupportedEntryError
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise InvalidChangeError("path_missing") from exc
    finally:
        os.close(dir_fd)


def exists_regular(base: Path, rel: str) -> bool:
    try:
        dir_fd, name = _open_parent(base, rel, create=False)
    except (FileNotFoundError, UnsafePathError):
        return False
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False
    finally:
        os.close(dir_fd)


def manifest(base: Path, *, max_entries: int, max_bytes: int) -> TreeManifest:
    """Hash every regular file. Any symlink, device or other entry fails closed."""
    entries: list[TreeEntry] = []
    total = 0
    for current, dirs, files in os.walk(base, followlinks=False):
        for name in [*dirs, *files]:
            info = os.lstat(os.path.join(current, name))
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
            ):
                raise UnsupportedEntryError
        for name in files:
            full = Path(current) / name
            rel = full.relative_to(base).as_posix()
            data = read_file(base, rel, max_bytes)
            total += len(data)
            if len(entries) + 1 > max_entries or total > max_bytes:
                raise WorkspaceLimitError
            executable = bool(os.lstat(full).st_mode & stat.S_IXUSR)
            entries.append(TreeEntry(rel, len(data), hashlib.sha256(data).hexdigest(), executable))
    return TreeManifest(tuple(sorted(entries, key=lambda e: e.path)))


def tree_size(base: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(base, followlinks=False):
        for name in files:
            total += os.lstat(os.path.join(current, name)).st_size
    return total
