"""The PostgreSQL registry wiring the remaining SQL integration tests use (deleted with PostgreSQL
in Task 16). The composition root builds the registry on DynamoDB only."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.git_providers.ports import GitProviderRegistry
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.persistence.repositories.registry_store import (
    SqlAlchemyRegistryUnitOfWork,
)


def sql_registry_service(
    sessions: async_sessionmaker[AsyncSession], providers: GitProviderRegistry | None = None
) -> RepositoryRegistryService:
    return RepositoryRegistryService(
        lambda: SqlAlchemyRegistryUnitOfWork(sessions), providers=providers
    )


def sql_repository_resolver(sessions: async_sessionmaker[AsyncSession]) -> RepositoryResolver:
    return RepositoryResolver(
        lambda: SqlAlchemyRegistryUnitOfWork(sessions), parsers=(GitHubReferenceParser(),)
    )
