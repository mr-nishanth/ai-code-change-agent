"""Baselines of registered repositories, read through the provider's verified baseline reader.

This is the production `BaselineProvider`: the repository is looked up in the registry by id (never
taken from a Story or a model), must be ACTIVE with the given published profile version, and is
read at the exact commit the attempt pinned. The Jira project mapping is checked by the resolver
before every workflow run and by the delivery service before every publish.
"""

import uuid
from collections.abc import AsyncIterator, Callable

from ai_code_change_agent.application.git_providers.baseline import (
    BaselineReaders,
    ProviderRepository,
    RepositoryBaselineReader,
    VerifiedBaseline,
)
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.application.workspaces.errors import BaselineError
from ai_code_change_agent.application.workspaces.models import BaselineFile
from ai_code_change_agent.application.workspaces.ports import BaselineSource
from ai_code_change_agent.domain.repositories.models import RepositoryStatus


class VerifiedBaselineSource:
    """A BaselineSource over an already verified, immutable set of files."""

    def __init__(self, baseline: VerifiedBaseline) -> None:
        self.baseline = baseline

    async def files(self) -> AsyncIterator[BaselineFile]:
        for f in self.baseline.files:
            yield f


class RegistryBaselineProvider:
    def __init__(
        self, uow_factory: Callable[[], RegistryUnitOfWork], readers: BaselineReaders
    ) -> None:
        self._uow = uow_factory
        self._readers = readers

    async def _target(
        self, repository_id: uuid.UUID, profile_version_id: uuid.UUID
    ) -> tuple[RepositoryBaselineReader, ProviderRepository]:
        async with self._uow() as uow:
            repo = await uow.store.get_repository_by_id(repository_id)
            if repo is None or repo.status is not RepositoryStatus.ACTIVE:
                raise BaselineError("repository_not_authorised")
            version = await uow.store.get_profile_version_by_id(profile_version_id)
            if version is None or version.repository_id != repo.id or not version.published:
                raise BaselineError("profile_invalid")
        reader = self._readers.get(repo.git_provider)
        if reader is None:
            raise BaselineError("baseline_provider_unavailable")  # no fallback to another
        return reader, ProviderRepository(
            git_provider=repo.git_provider,
            provider_repository_id=repo.provider_repository_id,
            owner=repo.owner,
            name=repo.name,
            default_branch=repo.default_branch,
        )

    async def pin(self, *, repository_id: uuid.UUID, profile_version_id: uuid.UUID) -> str:
        reader, target = await self._target(repository_id, profile_version_id)
        return await reader.pin(target)

    async def read(
        self, *, repository_id: uuid.UUID, profile_version_id: uuid.UUID, base_commit_sha: str
    ) -> VerifiedBaseline:
        reader, target = await self._target(repository_id, profile_version_id)
        baseline = await reader.read(target, base_commit_sha)
        if baseline.commit_sha != base_commit_sha:
            raise BaselineError("baseline_mismatch")  # a reader must never switch commits
        return baseline

    async def baseline_for(
        self, *, repository_id: uuid.UUID, profile_version_id: uuid.UUID, base_commit_sha: str
    ) -> BaselineSource:
        return VerifiedBaselineSource(
            await self.read(
                repository_id=repository_id,
                profile_version_id=profile_version_id,
                base_commit_sha=base_commit_sha,
            )
        )
