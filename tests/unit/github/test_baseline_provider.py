"""Gate D.2: registry-backed baselines, the pinned codebase view and model-content safety."""

import uuid
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.application.git_providers.baseline import (
    BaselineReaders,
    ProviderRepository,
    VerifiedBaseline,
)
from ai_code_change_agent.application.workspaces.errors import BaselineError
from ai_code_change_agent.application.workspaces.models import BaselineFile
from ai_code_change_agent.application.workspaces.registry_baseline import RegistryBaselineProvider
from ai_code_change_agent.domain.repositories.models import RepositoryStatus
from tests.support.github_repos import FILES, reader_rig
from tests.support.registry_fixtures import django_service, new_repo
from tests.support.registry_memory import InMemoryRegistry

GITHUB = {"provider": "github", "pid": "424242", "owner": "acme", "name": "shop-service"}
SHA = "a" * 40


class StubReader:
    def __init__(self, files: tuple[BaselineFile, ...] = (), commit: str | None = None) -> None:
        self.files = files
        self.commit = commit
        self.pins: list[ProviderRepository] = []
        self.reads: list[tuple[ProviderRepository, str]] = []

    async def pin(self, repository: ProviderRepository) -> str:
        self.pins.append(repository)
        return SHA

    async def read(self, repository: ProviderRepository, commit_sha: str) -> VerifiedBaseline:
        self.reads.append((repository, commit_sha))
        return VerifiedBaseline(self.commit or commit_sha, self.files)


class _Chg:
    def __init__(self, registry: InMemoryRegistry) -> None:
        self.registry = registry


async def setup(reader: Any, key: str = "github") -> tuple[_Chg, Any, Any, Any]:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(
        new_repo("shop-service", pid="424242", owner="acme", name="shop-service"), actor="cli:t"
    )
    await svc.create_version(
        "shop-service", django_service() | {"repository_key": "shop-service"}, actor="cli:t"
    )
    await svc.publish_version("shop-service", 1, actor="cli:t")
    await svc.map_project("SHOP", "shop-service", actor="cli:t")
    await svc.enable("shop-service", actor="cli:t")
    async with reg.uow() as uow:
        (repo,) = await uow.store.list_repositories()
        (version,) = await uow.store.list_profile_versions(repo.id)
    provider = RegistryBaselineProvider(reg.uow, BaselineReaders({key: reader}))
    return _Chg(reg), provider, repo, version


async def test_the_registered_identity_is_read_at_the_given_commit() -> None:
    reader = StubReader((BaselineFile("a.txt", b"a"),))
    _chg, provider, repo, version = await setup(reader)
    source = await provider.baseline_for(
        repository_id=repo.id, profile_version_id=version.id, base_commit_sha=SHA
    )
    assert [f.path async for f in source.files()] == ["a.txt"]
    ((target, sha),) = reader.reads
    assert sha == SHA and target == ProviderRepository(
        "github", "424242", "acme", "shop-service", "main"
    )
    assert await provider.pin(repository_id=repo.id, profile_version_id=version.id) == SHA


async def test_a_disabled_repository_is_never_read() -> None:
    reader = StubReader()
    chg, provider, repo, version = await setup(reader)
    async with chg.registry.uow() as uow:
        await uow.store.set_status(repo.id, RepositoryStatus.DISABLED)
        await uow.commit()
    with pytest.raises(BaselineError) as err:
        await provider.pin(repository_id=repo.id, profile_version_id=version.id)
    assert err.value.code == "repository_not_authorised" and reader.pins == []


async def test_an_unknown_repository_or_foreign_profile_is_refused() -> None:
    reader = StubReader()
    _chg, provider, repo, version = await setup(reader)
    with pytest.raises(BaselineError) as err:
        await provider.pin(repository_id=uuid.uuid4(), profile_version_id=version.id)
    assert err.value.code == "repository_not_authorised"
    with pytest.raises(BaselineError) as err:
        await provider.pin(repository_id=repo.id, profile_version_id=uuid.uuid4())
    assert err.value.code == "profile_invalid" and reader.pins == []


async def test_there_is_no_fallback_to_another_provider() -> None:
    _chg, provider, repo, version = await setup(StubReader(), key="gitlab")
    with pytest.raises(BaselineError) as err:
        await provider.pin(repository_id=repo.id, profile_version_id=version.id)
    assert err.value.code == "baseline_provider_unavailable"


async def test_a_reader_returning_another_commit_is_refused() -> None:
    _chg, provider, repo, version = await setup(StubReader(commit="b" * 40))
    with pytest.raises(BaselineError) as err:
        await provider.read(
            repository_id=repo.id, profile_version_id=version.id, base_commit_sha=SHA
        )
    assert err.value.code == "baseline_mismatch"


async def test_the_real_reader_behind_the_registry(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    _chg, provider, repo, version = await setup(rig.reader)
    sha = await provider.pin(repository_id=repo.id, profile_version_id=version.id)
    baseline = await provider.read(
        repository_id=repo.id, profile_version_id=version.id, base_commit_sha=sha
    )
    assert sha == rig.sha and {f.path for f in baseline.files} == set(FILES)
