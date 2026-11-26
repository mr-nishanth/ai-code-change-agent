"""Reading a published repository profile version by id, through the registry unit of work."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ai_code_change_agent.application.errors import WorkflowPreconditionError
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.domain.repositories.templates import with_default_test_command


@dataclass(frozen=True)
class LoadedProfile:
    version_id: uuid.UUID
    version: int
    sha256: str
    profile: RepositoryProfile


class ProfileReader:
    def __init__(self, uow_factory: Callable[[], RegistryUnitOfWork]) -> None:
        self._uow = uow_factory

    async def get(self, version_id: uuid.UUID) -> LoadedProfile:
        async with self._uow() as uow:
            row = await uow.store.get_profile_version_by_id(version_id)
        if row is None or row.published_at is None:
            raise WorkflowPreconditionError("profile_version_not_published")
        try:
            profile = with_default_test_command(RepositoryProfile.model_validate(row.content))
        except ValueError as exc:
            raise WorkflowPreconditionError("profile_invalid") from exc
        return LoadedProfile(row.id, row.version, row.content_sha256, profile)
