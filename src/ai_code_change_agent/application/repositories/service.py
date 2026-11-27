"""Registry use cases. Every mutation runs in one transaction together with its audit event.

A mutation that loses an optimistic race (`ConcurrentModificationError`: an item its unit of work
read changed before it committed) is retried as a whole method, up to `RETRY_ATTEMPTS` times with
full jitter; this reproduces waiting for the row lock (DYNAMODB-DATA-MODEL.md 5.1). Genuine
conflicts (`alias_taken`, `repository_already_registered`, ...) are never retried."""

import asyncio
import functools
import random
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Concatenate

from ai_code_change_agent.application.git_providers.ports import (
    GitProviderRegistry,
    ProviderRepositoryInfo,
)
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.application.repositories.validation import ProfileValidator
from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.repositories.errors import (
    ConflictError,
    Finding,
    InvalidStateError,
    NotFoundError,
    ProviderMismatchError,
    RegistryValidationError,
)
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    AuditEvent,
    MappingStatus,
    NewRepository,
    ProfileVersion,
    ProjectMapping,
    Repository,
    RepositoryStatus,
    new_repository_findings,
    normalise_alias,
    safe_metadata,
    validate_actor,
    validate_project_key,
)

UowFactory = Callable[[], RegistryUnitOfWork]
Sleep = Callable[[float], Awaitable[None]]

RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY = 0.025
RETRY_MAX_DELAY = 0.5


def _retry_delay(retry: int) -> float:
    """Full jitter: U(0, min(max, base * 2**retry))."""
    cap = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * float(2**retry))
    return random.random() * cap  # noqa: S311 - retry jitter, not a secret


def _retry_concurrent[**P, T](
    method: Callable[Concatenate["RepositoryRegistryService", P], Coroutine[Any, Any, T]],
) -> Callable[Concatenate["RepositoryRegistryService", P], Coroutine[Any, Any, T]]:
    """Run the whole method again (new unit of work, fresh reads) after a lost optimistic race."""

    @functools.wraps(method)
    async def wrapper(
        service: "RepositoryRegistryService", /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        for retry in range(RETRY_ATTEMPTS):
            try:
                return await method(service, *args, **kwargs)
            except ConcurrentModificationError:
                if retry + 1 >= RETRY_ATTEMPTS:
                    raise
            await service._sleep(_retry_delay(retry))
        raise AssertionError("unreachable")  # pragma: no cover

    return wrapper


class RepositoryRegistryService:
    def __init__(
        self,
        uow_factory: UowFactory,
        validator: ProfileValidator | None = None,
        providers: GitProviderRegistry | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._uow = uow_factory
        self._sleep = sleep
        self._validator = validator or ProfileValidator()
        self._providers = providers or GitProviderRegistry()

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    async def _audit(
        uow: RegistryUnitOfWork,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID | None,
        actor: str,
        correlation_id: str | None,
        **metadata: Any,
    ) -> None:
        await uow.audit.record(
            AuditEvent(
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                actor=actor,
                metadata=safe_metadata(metadata),
                correlation_id=correlation_id,
            )
        )

    @staticmethod
    async def _require(uow: RegistryUnitOfWork, key: str, *, lock: bool = False) -> Repository:
        repo = await (uow.store.lock_repository(key) if lock else uow.store.get_repository(key))
        if repo is None:
            raise NotFoundError("repository_not_found")
        return repo

    # ---- repositories --------------------------------------------------------------------
    async def describe_provider_repository(
        self, git_provider: str, *, owner: str, name: str, access_reference: str
    ) -> ProviderRepositoryInfo:
        """Read-only lookup of the provider's identity (numeric id, canonical owner/name, default
        branch) for operator registration. Changes nothing."""
        client = self._providers.get(git_provider)
        if client is None:
            raise ProviderMismatchError("provider_client_unavailable")
        return await client.describe_repository(
            owner=owner, name=name, access_reference=access_reference
        )

    @_retry_concurrent
    async def register_repository(
        self, new: NewRepository, *, actor: str, correlation_id: str | None = None
    ) -> Repository:
        validate_actor(actor)
        findings = new_repository_findings(new)
        if findings:
            raise RegistryValidationError(findings)
        verified = False
        client = self._providers.get(new.git_provider)
        if client is not None:
            info = await client.describe_repository(
                owner=new.owner, name=new.name, access_reference=new.access_reference
            )
            if (
                info.provider_repository_id != new.provider_repository_id
                or info.owner.lower() != new.owner.lower()
                or info.name.lower() != new.name.lower()
            ):
                raise ProviderMismatchError("provider_identity_mismatch")
            verified = True
        async with self._uow() as uow:
            repo = await uow.store.add_repository(new, created_by=actor)
            await self._audit(
                uow,
                AuditAction.REPOSITORY_REGISTERED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=repo.repository_key,
                git_provider=repo.git_provider,
                provider_verified=verified,
            )
            await uow.commit()
        return repo

    async def list_repositories(self) -> list[Repository]:
        async with self._uow() as uow:
            return await uow.store.list_repositories()

    async def show_repository(
        self, key: str
    ) -> tuple[Repository, list[ProfileVersion], list[str], list[ProjectMapping]]:
        async with self._uow() as uow:
            repo = await self._require(uow, key)
            return (
                repo,
                await uow.store.list_profile_versions(repo.id),
                await uow.store.list_aliases(repo.id),
                await uow.store.list_mappings(repository_id=repo.id),
            )

    @_retry_concurrent
    async def enable(
        self, key: str, *, actor: str, correlation_id: str | None = None
    ) -> Repository:
        validate_actor(actor)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            if repo.status is RepositoryStatus.ACTIVE:
                raise InvalidStateError("already_active")
            if repo.active_profile_version_id is None:
                raise InvalidStateError("no_active_profile_version")
            repo = await uow.store.set_status(repo.id, RepositoryStatus.ACTIVE)
            await self._audit(
                uow,
                AuditAction.REPOSITORY_ENABLED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
            )
            await uow.commit()
        return repo

    @_retry_concurrent
    async def disable(
        self, key: str, *, actor: str, correlation_id: str | None = None
    ) -> Repository:
        validate_actor(actor)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            if repo.status is RepositoryStatus.DISABLED:
                raise InvalidStateError("already_disabled")
            repo = await uow.store.set_status(repo.id, RepositoryStatus.DISABLED)
            await self._audit(
                uow,
                AuditAction.REPOSITORY_DISABLED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
            )
            await uow.commit()
        return repo

    @_retry_concurrent
    async def add_alias(
        self, key: str, alias: str, *, actor: str, correlation_id: str | None = None
    ) -> str:
        validate_actor(actor)
        normalised = normalise_alias(alias)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            await uow.store.add_alias(repo.id, normalised, actor)
            await self._audit(
                uow,
                AuditAction.ALIAS_ADDED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                alias=normalised,
            )
            await uow.commit()
        return normalised

    # ---- profile versions ----------------------------------------------------------------
    @_retry_concurrent
    async def create_version(
        self, key: str, document: object, *, actor: str, correlation_id: str | None = None
    ) -> ProfileVersion:
        validate_actor(actor)
        validated = self._validator.validate(document)
        if validated.profile.repository_key != key:
            raise RegistryValidationError(
                [
                    Finding(
                        "repository_key",
                        "key_mismatch",
                        "profile repository_key must equal the target repository",
                    )
                ]
            )
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            version = await uow.store.add_profile_version(
                repo.id,
                content=validated.content,
                content_sha256=validated.content_sha256,
                schema_version=validated.schema_version,
                policy_version=validated.policy_version,
                created_by=actor,
            )
            await self._audit(
                uow,
                AuditAction.PROFILE_VERSION_CREATED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                version=version.version,
                content_sha256=version.content_sha256,
            )
            await uow.commit()
        return version

    async def _published_valid_version(
        self, uow: RegistryUnitOfWork, repo: Repository, version_no: int
    ) -> ProfileVersion:
        version = await uow.store.get_profile_version(repo.id, version_no)
        if version is None:
            raise NotFoundError("profile_version_not_found")
        # fail closed: re-check against the current platform policy before any promotion
        self._validator.validate(version.content)
        return version

    @_retry_concurrent
    async def publish_version(
        self,
        key: str,
        version_no: int,
        *,
        actor: str,
        activate: bool = True,
        correlation_id: str | None = None,
    ) -> ProfileVersion:
        validate_actor(actor)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            version = await self._published_valid_version(uow, repo, version_no)
            if version.published:
                raise InvalidStateError("already_published")
            version = await uow.store.mark_published(version.id)
            await self._audit(
                uow,
                AuditAction.PROFILE_VERSION_PUBLISHED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                version=version.version,
            )
            if activate:
                await uow.store.set_active_version(repo.id, version.id)
                await self._audit(
                    uow,
                    AuditAction.PROFILE_VERSION_ACTIVATED,
                    "repository",
                    repo.id,
                    actor,
                    correlation_id,
                    repository_key=key,
                    version=version.version,
                )
            await uow.commit()
        return version

    @_retry_concurrent
    async def activate_version(
        self, key: str, version_no: int, *, actor: str, correlation_id: str | None = None
    ) -> Repository:
        validate_actor(actor)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            version = await self._published_valid_version(uow, repo, version_no)
            if not version.published:
                raise InvalidStateError("version_not_published")
            repo = await uow.store.set_active_version(repo.id, version.id)
            await self._audit(
                uow,
                AuditAction.PROFILE_VERSION_ACTIVATED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                version=version.version,
            )
            await uow.commit()
        return repo

    # ---- project mappings ----------------------------------------------------------------
    @_retry_concurrent
    async def map_project(
        self, project_key: str, key: str, *, actor: str, correlation_id: str | None = None
    ) -> ProjectMapping:
        validate_actor(actor)
        validate_project_key(project_key)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            existing = await uow.store.list_mappings(
                jira_project_key=project_key, repository_id=repo.id
            )
            if existing and existing[0].status is MappingStatus.ACTIVE:
                raise ConflictError("mapping_already_active")
            mapping, _ = await uow.store.set_mapping(
                project_key, repo.id, MappingStatus.ACTIVE, actor
            )
            await self._audit(
                uow,
                AuditAction.PROJECT_MAPPED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                jira_project_key=project_key,
            )
            await uow.commit()
        return mapping

    @_retry_concurrent
    async def unmap_project(
        self, project_key: str, key: str, *, actor: str, correlation_id: str | None = None
    ) -> ProjectMapping:
        validate_actor(actor)
        validate_project_key(project_key)
        async with self._uow() as uow:
            repo = await self._require(uow, key, lock=True)
            existing = await uow.store.list_mappings(
                jira_project_key=project_key, repository_id=repo.id
            )
            if not existing or existing[0].status is not MappingStatus.ACTIVE:
                raise InvalidStateError("mapping_not_active")
            mapping, _ = await uow.store.set_mapping(
                project_key, repo.id, MappingStatus.DISABLED, actor
            )
            await self._audit(
                uow,
                AuditAction.PROJECT_UNMAPPED,
                "repository",
                repo.id,
                actor,
                correlation_id,
                repository_key=key,
                jira_project_key=project_key,
            )
            await uow.commit()
        return mapping

    async def list_mappings(
        self, *, project_key: str | None = None, key: str | None = None
    ) -> list[ProjectMapping]:
        async with self._uow() as uow:
            repo_id = (await self._require(uow, key)).id if key else None
            return await uow.store.list_mappings(
                jira_project_key=project_key, repository_id=repo_id
            )

    async def audit_history(self, key: str | None = None, limit: int = 50) -> list[AuditEvent]:
        async with self._uow() as uow:
            repo_id = (await self._require(uow, key)).id if key else None
            return await uow.audit.list_events(
                entity_type="repository" if key else None, entity_id=repo_id, limit=min(limit, 500)
            )
