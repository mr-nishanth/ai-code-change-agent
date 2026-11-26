"""GitHub App credentials.

The private key is read once from the file named by APP_GITHUB_PRIVATE_KEY_PATH (never from an
environment value, a Story, the registry or a model), validated, and kept as a key object inside
this package. Errors carry fixed codes only: never the path, the file content or a parser message.
"""

import os
import stat
from dataclasses import dataclass, field

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

MAX_KEY_BYTES = 16 * 1024
MIN_KEY_BITS = 2048


class GitHubAppConfigError(Exception):
    """Configuration is missing or invalid. The message is the fixed code, nothing else."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GitHubAppCredentials:
    app_id: int
    installation_id: int
    private_key: RSAPrivateKey = field(repr=False)

    def __repr__(self) -> str:  # never render the key object
        return f"GitHubAppCredentials(app_id={self.app_id}, installation_id=...)"


def load_private_key(path: str) -> RSAPrivateKey:
    """An RSA PEM private key (>= 2048 bits) in a regular file owned by this user, readable by
    nobody else (mode 0600 or 0400), at most 16 KiB, reached without symlinks."""
    if not path.startswith("/") or ".." in path.split("/") or os.path.realpath(path) != path:
        raise GitHubAppConfigError("github_app_config_invalid")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise GitHubAppConfigError("private_key_unavailable") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_KEY_BYTES
        ):
            raise GitHubAppConfigError("private_key_unavailable")
        data = os.read(fd, MAX_KEY_BYTES + 1)
    finally:
        os.close(fd)
    try:
        key = load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise GitHubAppConfigError("private_key_unavailable") from None
    if not isinstance(key, RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        raise GitHubAppConfigError("private_key_unavailable")
    return key


def load_credentials(
    app_id: int | None, installation_id: int | None, private_key_path: str | None
) -> GitHubAppCredentials:
    if app_id is None or installation_id is None or private_key_path is None:
        raise GitHubAppConfigError("github_app_config_invalid")
    if app_id < 1 or installation_id < 1:
        raise GitHubAppConfigError("github_app_config_invalid")
    return GitHubAppCredentials(app_id, installation_id, load_private_key(private_key_path))
