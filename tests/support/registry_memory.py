"""In-memory registry double implementing the application ports (unit tests only)."""

import copy
import dataclasses
import uuid
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self

from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.domain.repositories.models import (
    AuditEvent,
    MappingStatus,
    NewRepository,
    ProfileVersion,
    ProjectMapping,
    Repository,
    RepositoryStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


class _State:
    def __init__(self) -> None:
        self.repos: dict[uuid.UUID, Repository] = {}
        self.aliases: dict[str, uuid.UUID] = {}
        self.versions: dict[uuid.UUID, ProfileVersion] = {}
        self.mappings: dict[tuple[str, uuid.UUID], ProjectMapping] = {}
        self.audit: list[AuditEvent] = []


class _Store:
    def __init__(self, s: _State) -> None:
        self.s = s

    async def add_repository(self, new: NewRepository, *, created_by: str) -> Repository:
        for r in self.s.repos.values():
            if (
                r.repository_key == new.repository_key
                or r.canonical_locator == new.canonical_locator
                or (r.git_provider, r.provider_repository_id)
                == (new.git_provider, new.provider_repository_id)
            ):
                raise ConflictError("repository_already_registered")
        if new.repository_key in self.s.aliases:
            raise ConflictError("repository_key_taken")
        repo = Repository(
            id=uuid.uuid4(), repository_key=new.repository_key, display_name=new.display_name,
            git_provider=new.git_provider, provider_repository_id=new.provider_repository_id,
            owner=new.owner, name=new.name, canonical_locator=new.canonical_locator,
            default_branch=new.default_branch, status=RepositoryStatus.DISABLED,
            access_reference=new.access_reference, active_profile_version_id=None,
            row_version=1, created_at=_now(), updated_at=_now(),
        )  # fmt: skip
        self.s.repos[repo.id] = repo
        self.s.aliases[new.repository_key] = repo.id
        return repo

    async def get_repository(self, key: str) -> Repository | None:
        return next((r for r in self.s.repos.values() if r.repository_key == key), None)

    lock_repository = get_repository

    async def get_repository_by_id(self, repository_id: uuid.UUID) -> Repository | None:
        return self.s.repos.get(repository_id)

    async def list_repositories(self) -> list[Repository]:
        return sorted(self.s.repos.values(), key=lambda r: r.repository_key)

    def _replace(self, repository_id: uuid.UUID, **kw: Any) -> Repository:
        old = self.s.repos[repository_id]
        new = Repository(**{**old.__dict__, **kw, "row_version": old.row_version + 1})
        self.s.repos[repository_id] = new
        return new

    async def set_status(self, repository_id: uuid.UUID, status: RepositoryStatus) -> Repository:
        return self._replace(repository_id, status=status)

    async def add_alias(self, repository_id: uuid.UUID, alias: str, created_by: str) -> None:
        if alias in self.s.aliases:
            raise ConflictError("alias_taken")
        self.s.aliases[alias] = repository_id

    async def list_aliases(self, repository_id: uuid.UUID) -> list[str]:
        return sorted(a for a, r in self.s.aliases.items() if r == repository_id)

    async def find_by_alias(self, alias: str) -> Repository | None:
        rid = self.s.aliases.get(alias)
        return self.s.repos.get(rid) if rid else None

    async def find_by_locator(self, locator: str) -> Repository | None:
        return next((r for r in self.s.repos.values() if r.canonical_locator == locator), None)

    async def add_profile_version(
        self, repository_id: uuid.UUID, *, content: dict[str, Any], content_sha256: str,
        schema_version: int, policy_version: str, created_by: str,
    ) -> ProfileVersion:  # fmt: skip
        n = 1 + max(
            (v.version for v in self.s.versions.values() if v.repository_id == repository_id),
            default=0,
        )
        v = ProfileVersion(
            id=uuid.uuid4(), repository_id=repository_id, version=n, schema_version=schema_version,
            content=copy.deepcopy(content), content_sha256=content_sha256,
            policy_version=policy_version, created_by=created_by, created_at=_now(), published_at=None,
        )  # fmt: skip
        self.s.versions[v.id] = v
        return v

    async def get_profile_version(
        self, repository_id: uuid.UUID, version: int
    ) -> ProfileVersion | None:
        return next(
            (
                v
                for v in self.s.versions.values()
                if v.repository_id == repository_id and v.version == version
            ),
            None,
        )

    async def get_profile_version_by_id(self, version_id: uuid.UUID) -> ProfileVersion | None:
        return self.s.versions.get(version_id)

    async def list_profile_versions(self, repository_id: uuid.UUID) -> list[ProfileVersion]:
        return sorted(
            (v for v in self.s.versions.values() if v.repository_id == repository_id),
            key=lambda v: v.version,
        )

    async def mark_published(self, version_id: uuid.UUID) -> ProfileVersion:
        v = self.s.versions[version_id]
        new = ProfileVersion(**{**v.__dict__, "published_at": _now()})
        self.s.versions[version_id] = new
        return new

    async def set_active_version(
        self, repository_id: uuid.UUID, version_id: uuid.UUID
    ) -> Repository:
        return self._replace(repository_id, active_profile_version_id=version_id)

    async def set_mapping(
        self,
        jira_project_key: str,
        repository_id: uuid.UUID,
        status: MappingStatus,
        created_by: str,
    ) -> tuple[ProjectMapping, bool]:
        key = (jira_project_key, repository_id)
        old = self.s.mappings.get(key)
        m = ProjectMapping(
            id=old.id if old else uuid.uuid4(), jira_project_key=jira_project_key,
            repository_id=repository_id, status=status,
            created_by=old.created_by if old else created_by, created_at=old.created_at if old else _now(),
        )  # fmt: skip
        self.s.mappings[key] = m
        return m, old is None

    async def list_mappings(
        self, *, jira_project_key: str | None = None, repository_id: uuid.UUID | None = None,
        status: MappingStatus | None = None,
    ) -> list[ProjectMapping]:  # fmt: skip
        return [
            m for m in self.s.mappings.values()
            if (jira_project_key is None or m.jira_project_key == jira_project_key)
            and (repository_id is None or m.repository_id == repository_id)
            and (status is None or m.status is status)
        ]  # fmt: skip


class _Audit:
    def __init__(self, s: _State) -> None:
        self.s = s

    async def record(self, event: AuditEvent) -> None:
        self.s.audit.append(dataclasses.replace(event, occurred_at=_now()))

    async def list_events(
        self, *, entity_type: str | None = None, entity_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[AuditEvent]:
        return [
            e for e in reversed(self.s.audit) if (entity_id is None or e.entity_id == entity_id)
        ][:limit]


class InMemoryRegistry:
    """Shared state; each unit of work snapshots and restores it unless commit() was called."""

    def __init__(self) -> None:
        self.state = _State()

    def uow(self) -> "_Uow":
        return _Uow(self.state)

    def service(self, **kw: Any) -> RepositoryRegistryService:
        return RepositoryRegistryService(self.uow, **kw)


class _Uow:
    def __init__(self, state: _State) -> None:
        self._state = state
        self._snapshot: dict[str, Any] = {}
        self._committed = False
        self.store = _Store(state)
        self.audit = _Audit(state)

    async def __aenter__(self) -> Self:
        self._snapshot = {k: copy.copy(v) for k, v in self._state.__dict__.items()}
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        if not self._committed:
            self._state.__dict__.update(self._snapshot)

    async def commit(self) -> None:
        self._committed = True
