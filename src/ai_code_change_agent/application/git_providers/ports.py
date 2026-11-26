"""Minimum provider contract needed by the registry. Real adapters arrive in later modules.

Deliberately absent: clone, branch, commit, push and pull-request operations.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderRepositoryInfo:
    provider_repository_id: str
    owner: str
    name: str
    default_branch: str


class GitProviderClient(Protocol):
    async def describe_repository(
        self, *, owner: str, name: str, access_reference: str
    ) -> ProviderRepositoryInfo:
        """Return the provider's own identity for the repository."""
        ...


class GitProviderRegistry:
    """Maps an opaque provider key to its adapter. Built in the composition root."""

    def __init__(self, clients: dict[str, GitProviderClient] | None = None) -> None:
        self._clients = dict(clients or {})

    def register(self, key: str, client: GitProviderClient) -> None:
        self._clients[key] = client

    def get(self, key: str) -> GitProviderClient | None:
        return self._clients.get(key)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._clients))
