"""Port for trusted dependency preparation (ADR-010). The coding sandbox never fetches packages."""

from dataclasses import dataclass
from typing import Protocol

from ai_code_change_agent.domain.dependencies.policy import DependencyRequest


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    filename: str
    sha256: str
    size: int


@dataclass(frozen=True)
class DependencyBundle:
    """An immutable, verified bundle: hash-locked wheels installed (offline) into `site_dir`,
    which sandboxes and validation containers mount read-only."""

    key: str
    lock_sha256: str
    site_dir: str
    packages: tuple[LockedPackage, ...]


class DependencyPreparer(Protocol):
    async def prepare(self, requests: tuple[DependencyRequest, ...]) -> DependencyBundle | None:
        """None when nothing was requested. Raises an error carrying a `dependency_*` code."""
        ...
