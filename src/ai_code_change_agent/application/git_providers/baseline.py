"""Baseline reader port: one exact commit of a registered repository, fully verified.

A reader receives only the registry's identity of the repository (never a Story or model value),
resolves the registered default branch to one commit SHA exactly once (`pin`) and afterwards
reads only that commit (`read`). It returns every regular file of the commit or raises
`BaselineError`: a partial snapshot is never returned.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from ai_code_change_agent.application.workspaces.models import BaselineFile
from ai_code_change_agent.application.workspaces.paths import tree_hash

# Fixed snapshot limits (owner decision D2.6). Exceeding any of them fails the snapshot.
MAX_SNAPSHOT_FILES = 5_000
MAX_SNAPSHOT_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_TOTAL_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ProviderRepository:
    """The registered identity of a repository, read from the registry by the platform."""

    git_provider: str
    provider_repository_id: str
    owner: str
    name: str
    default_branch: str


@dataclass(frozen=True)
class VerifiedBaseline:
    """Every regular file of one commit, verified against the provider's Git object hashes."""

    commit_sha: str
    files: tuple[BaselineFile, ...]  # sorted by path, unique paths

    @property
    def tree_hash(self) -> str:
        """The platform tree hash (same contract as TreeManifest.tree_hash)."""
        return tree_hash(
            (f.path, f.executable, hashlib.sha256(f.content).hexdigest()) for f in self.files
        )

    @property
    def total_bytes(self) -> int:
        return sum(len(f.content) for f in self.files)


class RepositoryBaselineReader(Protocol):
    async def pin(self, repository: ProviderRepository) -> str:
        """Resolve the registered default branch to one commit SHA (called once per attempt)."""
        ...

    async def read(self, repository: ProviderRepository, commit_sha: str) -> VerifiedBaseline:
        """Read and verify exactly this commit."""
        ...


class BaselineReaders:
    """Maps the registry's git_provider key to its reader. No fallback to another provider."""

    def __init__(self, readers: dict[str, RepositoryBaselineReader] | None = None) -> None:
        self._readers = dict(readers or {})

    def register(self, key: str, reader: RepositoryBaselineReader) -> None:
        self._readers[key] = reader

    def get(self, key: str) -> RepositoryBaselineReader | None:
        return self._readers.get(key)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._readers))
