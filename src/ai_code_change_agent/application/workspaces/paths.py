"""Path rules shared by every workspace operation and by plan validation."""

import hashlib
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from ai_code_change_agent.application.workspaces.errors import UnsafePathError

MAX_PATH_CHARS = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")

# Files that carry platform, CI, container or credential authority. A model may never change them
# (the platform policy decides; there is no per-change override in 05B). The rule is generic:
# every hidden file or directory (names starting with ".": CI definitions, tool and credential
# configuration, VCS metadata) plus the container, pipeline and key-material patterns below.
_PROTECTED_NAMES = frozenset(
    {"jenkinsfile", "id_rsa", "id_ed25519", "known_hosts", "authorized_keys"}
)
_PROTECTED_PREFIXES = ("dockerfile", "docker-compose", "compose.")
_PROTECTED_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    "-ci.yml",
    "-ci.yaml",
    "pipelines.yml",
    "pipelines.yaml",
)


def validate_relative_path(value: str) -> str:
    """A repository-relative POSIX path: no absolute form, no traversal, no control characters."""
    path = value.strip()
    parts = path.split("/")
    if (
        not path
        or len(path) > MAX_PATH_CHARS
        or path.startswith(("/", "~"))
        or "\\" in path
        or _CONTROL.search(path)
        or _DRIVE.match(path)
        or any(p in {"", ".", ".."} for p in parts)
    ):
        raise UnsafePathError
    return path


def is_protected(path: str) -> bool:
    parts = [p.lower() for p in PurePosixPath(path).parts]
    name = parts[-1]
    return (
        any(p.startswith(".") for p in parts)
        or name in _PROTECTED_NAMES
        or name.startswith(_PROTECTED_PREFIXES)
        or name.endswith(_PROTECTED_SUFFIXES)
    )


def tree_hash(entries: Iterable[tuple[str, bool, str]]) -> str:
    """SHA-256 over sorted (path, executable, content hash): independent of Git and timestamps."""
    digest = hashlib.sha256()
    for path, executable, sha in sorted(entries):
        digest.update(f"{path}\0{int(executable)}\0{sha}\n".encode())
    return digest.hexdigest()
