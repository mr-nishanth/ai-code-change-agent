"""PostgreSQL adapters for the registry ports (one SQLAlchemy session = one transaction)."""

import uuid
from types import TracebackType
from typing import Any, Self

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    AuditEvent,
    MappingStatus,
    NewRepository,
    ProfileVersion,
    ProjectMapping,
    Repository,
    RepositoryStatus,
)
from ai_code_change_agent.persistence.models.registry import (
    RegistryAuditEventModel,
    RepositoryAliasModel,
    RepositoryModel,
    RepositoryProfileVersionModel,
    RepositoryProjectMappingModel,
)


def _repo(m: RepositoryModel) -> Repository:
    return Repository(
        id=m.id,
        repository_key=m.repository_key,
        display_name=m.display_name,
        git_provider=m.git_provider,
        provider_repository_id=m.provider_repository_id,
        owner=m.owner,
        name=m.name,
        canonical_locator=m.canonical_locator,
        default_branch=m.default_branch,
        status=RepositoryStatus(m.status),
        access_reference=m.access_reference,
        active_profile_version_id=m.active_profile_version_id,
        row_version=m.row_version,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _version(m: RepositoryProfileVersionModel) -> ProfileVersion:
    return ProfileVersion(
        id=m.id,
        repository_id=m.repository_id,
        version=m.version,
        schema_version=m.schema_version,
        content=m.content,
        content_sha256=m.content_sha256,
        policy_version=m.policy_version,
        created_by=m.created_by,
        created_at=m.created_at,
        published_at=m.published_at,
    )


def _mapping(m: RepositoryProjectMappingModel) -> ProjectMapping:
    return ProjectMapping(
        id=m.id,
        jira_project_key=m.jira_project_key,
        repository_id=m.repository_id,
        status=MappingStatus(m.status),
        created_by=m.created_by,
        created_at=m.created_at,
    )


class SqlAlchemyRegistryStore:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def _flush(self, code: str) -> None:
        try:
            await self._s.flush()
        except IntegrityError:
            # constraint names and driver text are deliberately not surfaced
            raise ConflictError(code) from None

    async def add_repository(self, new: NewRepository, *, created_by: str) -> Repository:
        model = RepositoryModel(
            id=uuid.uuid4(),
            repository_key=new.repository_key,
            display_name=new.display_name,
            git_provider=new.git_provider,
            provider_repository_id=new.provider_repository_id,
            owner=new.owner,
            name=new.name,
            canonical_locator=new.canonical_locator,
            default_branch=new.default_branch,
            status="DISABLED",
            access_reference=new.access_reference,
            row_version=1,
            created_by=created_by,
        )
        self._s.add(model)
        await self._flush("repository_already_registered")
        self._s.add(
            RepositoryAliasModel(
                alias=new.repository_key, repository_id=model.id, kind="KEY", created_by=created_by
            )
        )
        await self._flush("repository_key_taken")
        await self._s.refresh(model)
        return _repo(model)

    async def get_repository(self, key: str) -> Repository | None:
        row = (
            await self._s.execute(
                select(RepositoryModel).where(RepositoryModel.repository_key == key)
            )
        ).scalar_one_or_none()
        return _repo(row) if row else None

    async def lock_repository(self, key: str) -> Repository | None:
        row = (
            await self._s.execute(
                select(RepositoryModel)
                .where(RepositoryModel.repository_key == key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _repo(row) if row else None

    async def get_repository_by_id(self, repository_id: uuid.UUID) -> Repository | None:
        row = await self._s.get(RepositoryModel, repository_id)
        return _repo(row) if row else None

    async def list_repositories(self) -> list[Repository]:
        rows = (
            await self._s.execute(select(RepositoryModel).order_by(RepositoryModel.repository_key))
        ).scalars()
        return [_repo(r) for r in rows]

    async def _bump(self, repository_id: uuid.UUID, **values: Any) -> Repository:
        await self._s.execute(
            update(RepositoryModel)
            .where(RepositoryModel.id == repository_id)
            .values(row_version=RepositoryModel.row_version + 1, updated_at=func.now(), **values)
        )
        await self._flush("repository_state_conflict")
        row = await self._s.get(RepositoryModel, repository_id, populate_existing=True)
        assert row is not None  # noqa: S101 - row was just updated in this transaction
        return _repo(row)

    async def set_status(self, repository_id: uuid.UUID, status: RepositoryStatus) -> Repository:
        return await self._bump(repository_id, status=status.value)

    async def add_alias(self, repository_id: uuid.UUID, alias: str, created_by: str) -> None:
        self._s.add(
            RepositoryAliasModel(
                alias=alias, repository_id=repository_id, kind="ALIAS", created_by=created_by
            )
        )
        await self._flush("alias_taken")

    async def list_aliases(self, repository_id: uuid.UUID) -> list[str]:
        rows = await self._s.execute(
            select(RepositoryAliasModel.alias)
            .where(RepositoryAliasModel.repository_id == repository_id)
            .order_by(RepositoryAliasModel.alias)
        )
        return [r[0] for r in rows]

    async def find_by_alias(self, alias: str) -> Repository | None:
        row = (
            await self._s.execute(
                select(RepositoryModel)
                .join(
                    RepositoryAliasModel, RepositoryAliasModel.repository_id == RepositoryModel.id
                )
                .where(RepositoryAliasModel.alias == alias)
            )
        ).scalar_one_or_none()
        return _repo(row) if row else None

    async def find_by_locator(self, locator: str) -> Repository | None:
        row = (
            await self._s.execute(
                select(RepositoryModel).where(RepositoryModel.canonical_locator == locator)
            )
        ).scalar_one_or_none()
        return _repo(row) if row else None

    async def add_profile_version(
        self,
        repository_id: uuid.UUID,
        *,
        content: dict[str, Any],
        content_sha256: str,
        schema_version: int,
        policy_version: str,
        created_by: str,
    ) -> ProfileVersion:
        # the caller holds a row lock on the repository, so max+1 is race free
        current = (
            await self._s.execute(
                select(func.coalesce(func.max(RepositoryProfileVersionModel.version), 0)).where(
                    RepositoryProfileVersionModel.repository_id == repository_id
                )
            )
        ).scalar_one()
        model = RepositoryProfileVersionModel(
            id=uuid.uuid4(),
            repository_id=repository_id,
            version=current + 1,
            schema_version=schema_version,
            content=content,
            content_sha256=content_sha256,
            policy_version=policy_version,
            created_by=created_by,
        )
        self._s.add(model)
        await self._flush("profile_version_conflict")
        await self._s.refresh(model)
        return _version(model)

    async def get_profile_version(
        self, repository_id: uuid.UUID, version: int
    ) -> ProfileVersion | None:
        row = (
            await self._s.execute(
                select(RepositoryProfileVersionModel).where(
                    RepositoryProfileVersionModel.repository_id == repository_id,
                    RepositoryProfileVersionModel.version == version,
                )
            )
        ).scalar_one_or_none()
        return _version(row) if row else None

    async def get_profile_version_by_id(self, version_id: uuid.UUID) -> ProfileVersion | None:
        row = await self._s.get(RepositoryProfileVersionModel, version_id)
        return _version(row) if row else None

    async def list_profile_versions(self, repository_id: uuid.UUID) -> list[ProfileVersion]:
        rows = (
            await self._s.execute(
                select(RepositoryProfileVersionModel)
                .where(RepositoryProfileVersionModel.repository_id == repository_id)
                .order_by(RepositoryProfileVersionModel.version)
            )
        ).scalars()
        return [_version(r) for r in rows]

    async def mark_published(self, version_id: uuid.UUID) -> ProfileVersion:
        await self._s.execute(
            update(RepositoryProfileVersionModel)
            .where(RepositoryProfileVersionModel.id == version_id)
            .values(published_at=func.now())
        )
        await self._flush("publish_conflict")
        row = await self._s.get(RepositoryProfileVersionModel, version_id, populate_existing=True)
        assert row is not None  # noqa: S101
        return _version(row)

    async def set_active_version(
        self, repository_id: uuid.UUID, version_id: uuid.UUID
    ) -> Repository:
        return await self._bump(repository_id, active_profile_version_id=version_id)

    async def set_mapping(
        self,
        jira_project_key: str,
        repository_id: uuid.UUID,
        status: MappingStatus,
        created_by: str,
    ) -> tuple[ProjectMapping, bool]:
        row = (
            await self._s.execute(
                select(RepositoryProjectMappingModel)
                .where(
                    RepositoryProjectMappingModel.jira_project_key == jira_project_key,
                    RepositoryProjectMappingModel.repository_id == repository_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = RepositoryProjectMappingModel(
                id=uuid.uuid4(),
                jira_project_key=jira_project_key,
                repository_id=repository_id,
                status=status.value,
                created_by=created_by,
            )
            self._s.add(row)
        else:
            row.status = status.value
            row.updated_at = func.now()
        await self._flush("mapping_conflict")
        await self._s.refresh(row)
        return _mapping(row), created

    async def list_mappings(
        self,
        *,
        jira_project_key: str | None = None,
        repository_id: uuid.UUID | None = None,
        status: MappingStatus | None = None,
    ) -> list[ProjectMapping]:
        stmt = select(RepositoryProjectMappingModel).order_by(
            RepositoryProjectMappingModel.jira_project_key,
            RepositoryProjectMappingModel.created_at,
        )
        if jira_project_key is not None:
            stmt = stmt.where(RepositoryProjectMappingModel.jira_project_key == jira_project_key)
        if repository_id is not None:
            stmt = stmt.where(RepositoryProjectMappingModel.repository_id == repository_id)
        if status is not None:
            stmt = stmt.where(RepositoryProjectMappingModel.status == status.value)
        return [_mapping(r) for r in (await self._s.execute(stmt)).scalars()]


class SqlAlchemyAuditLog:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, event: AuditEvent) -> None:
        self._s.add(
            RegistryAuditEventModel(
                id=event.id,
                actor=event.actor,
                action=event.action.value,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                correlation_id=event.correlation_id,
                event_metadata=dict(event.metadata),
            )
        )
        await self._s.flush()

    async def list_events(
        self, *, entity_type: str | None = None, entity_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[AuditEvent]:
        stmt = select(RegistryAuditEventModel).order_by(
            RegistryAuditEventModel.occurred_at.desc(), RegistryAuditEventModel.id
        )
        if entity_type is not None:
            stmt = stmt.where(RegistryAuditEventModel.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(RegistryAuditEventModel.entity_id == entity_id)
        rows = (await self._s.execute(stmt.limit(limit))).scalars()
        return [
            AuditEvent(
                id=r.id,
                action=AuditAction(r.action),
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                actor=r.actor,
                metadata=dict(r.event_metadata),
                correlation_id=r.correlation_id,
                occurred_at=r.occurred_at,
            )
            for r in rows
        ]


class SqlAlchemyRegistryUnitOfWork:
    """One transaction. Leaving the block without commit() rolls everything back."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory
        self._session: AsyncSession | None = None
        self.store: SqlAlchemyRegistryStore
        self.audit: SqlAlchemyAuditLog

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        self.store = SqlAlchemyRegistryStore(self._session)
        self.audit = SqlAlchemyAuditLog(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None  # noqa: S101
        try:
            await self._session.rollback()  # no-op after commit
        finally:
            await self._session.close()

    async def commit(self) -> None:
        assert self._session is not None  # noqa: S101
        await self._session.commit()
