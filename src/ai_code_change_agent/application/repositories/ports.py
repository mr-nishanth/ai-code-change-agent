"""Persistence ports for the registry (one unit of work = one transaction)."""

import uuid
from types import TracebackType
from typing import Any, Protocol, Self

from ai_code_change_agent.domain.repositories.models import (
    AuditEvent,
    MappingStatus,
    NewRepository,
    ProfileVersion,
    ProjectMapping,
    Repository,
    RepositoryStatus,
)


class RepositoryReferenceParser(Protocol):
    """Turns one provider's repository reference syntax (as written in a Story) into the registry's
    canonical locator. Pure string handling: the reference is never fetched or resolved over the
    network. Returns None when the value is not in this provider's syntax or is unsafe."""

    def locator(self, reference: str) -> str | None: ...


class RegistryStore(Protocol):
    async def add_repository(self, new: NewRepository, *, created_by: str) -> Repository: ...
    async def get_repository(self, key: str) -> Repository | None: ...
    async def get_repository_by_id(self, repository_id: uuid.UUID) -> Repository | None: ...
    async def lock_repository(self, key: str) -> Repository | None: ...
    async def list_repositories(self) -> list[Repository]: ...
    async def set_status(
        self, repository_id: uuid.UUID, status: RepositoryStatus
    ) -> Repository: ...
    async def add_alias(self, repository_id: uuid.UUID, alias: str, created_by: str) -> None: ...
    async def list_aliases(self, repository_id: uuid.UUID) -> list[str]: ...
    async def find_by_alias(self, alias: str) -> Repository | None: ...
    async def find_by_locator(self, locator: str) -> Repository | None: ...
    async def add_profile_version(
        self,
        repository_id: uuid.UUID,
        *,
        content: dict[str, Any],
        content_sha256: str,
        schema_version: int,
        policy_version: str,
        created_by: str,
    ) -> ProfileVersion: ...
    async def get_profile_version(
        self, repository_id: uuid.UUID, version: int
    ) -> ProfileVersion | None: ...
    async def get_profile_version_by_id(self, version_id: uuid.UUID) -> ProfileVersion | None: ...
    async def list_profile_versions(self, repository_id: uuid.UUID) -> list[ProfileVersion]: ...
    async def mark_published(self, version_id: uuid.UUID) -> ProfileVersion: ...
    async def set_active_version(
        self, repository_id: uuid.UUID, version_id: uuid.UUID
    ) -> Repository: ...
    async def set_mapping(
        self,
        jira_project_key: str,
        repository_id: uuid.UUID,
        status: MappingStatus,
        created_by: str,
    ) -> tuple[ProjectMapping, bool]:
        """Create or update the mapping; returns (mapping, created)."""
        ...

    async def list_mappings(
        self,
        *,
        jira_project_key: str | None = None,
        repository_id: uuid.UUID | None = None,
        status: MappingStatus | None = None,
    ) -> list[ProjectMapping]: ...


class AuditLog(Protocol):
    async def record(self, event: AuditEvent) -> None: ...
    async def list_events(
        self, *, entity_type: str | None = None, entity_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[AuditEvent]: ...


class RegistryUnitOfWork(Protocol):
    @property
    def store(self) -> RegistryStore: ...
    @property
    def audit(self) -> AuditLog: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
