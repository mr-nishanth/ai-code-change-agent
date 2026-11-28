"""RepositoryRegistryService retries a whole mutation after a lost optimistic race, and only then."""

import random

import pytest

from ai_code_change_agent.application.repositories import service as service_module
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.repositories.errors import ConflictError
from tests.support.registry_fixtures import new_repo
from tests.support.registry_memory import InMemoryRegistry

ACTOR = "cli:tester"


class LosingRaces:
    """A unit-of-work factory whose first `losses` commits lose an optimistic race."""

    def __init__(self, registry: InMemoryRegistry, losses: int) -> None:
        self.registry = registry
        self.losses = losses
        self.opened = 0

    def __call__(self) -> RegistryUnitOfWork:
        self.opened += 1
        uow = self.registry.uow()
        if self.losses:
            self.losses -= 1

            async def lost() -> None:
                raise ConcurrentModificationError()

            uow.commit = lost  # type: ignore[method-assign]
        return uow


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


async def test_a_lost_race_reruns_the_whole_method_in_a_new_unit_of_work() -> None:
    registry, sleeps = InMemoryRegistry(), Sleeps()
    factory = LosingRaces(registry, losses=2)
    svc = RepositoryRegistryService(factory, sleep=sleeps)

    repo = await svc.register_repository(new_repo("shop-api"), actor=ACTOR)

    assert factory.opened == 3 and len(sleeps.delays) == 2
    assert [r.id for r in await svc.list_repositories()] == [repo.id]
    assert len(await svc.audit_history()) == 1  # the lost attempts left nothing behind


async def test_retries_are_bounded_and_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "random", lambda: 1.0)
    registry, sleeps = InMemoryRegistry(), Sleeps()
    factory = LosingRaces(registry, losses=99)
    svc = RepositoryRegistryService(factory, sleep=sleeps)

    with pytest.raises(ConcurrentModificationError):
        await svc.register_repository(new_repo("shop-api"), actor=ACTOR)

    assert factory.opened == service_module.RETRY_ATTEMPTS == 5
    assert sleeps.delays == [0.025, 0.05, 0.1, 0.2]  # full jitter: U(0, min(0.5, 0.025 * 2**n))
    assert await svc.list_repositories() == []


async def test_genuine_conflicts_are_not_retried() -> None:
    registry, sleeps = InMemoryRegistry(), Sleeps()
    factory = LosingRaces(registry, losses=0)
    svc = RepositoryRegistryService(factory, sleep=sleeps)
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    opened = factory.opened

    with pytest.raises(ConflictError, match=r"^repository_already_registered$") as raised:
        await svc.register_repository(new_repo("shop-api"), actor=ACTOR)

    assert not isinstance(raised.value, ConcurrentModificationError)
    assert factory.opened == opened + 1 and sleeps.delays == []
