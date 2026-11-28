"""CodeChangeService retries a whole mutation after a lost optimistic race, and only then."""

import copy
import random
import uuid

import pytest

from ai_code_change_agent.application import retry as retry_module
from ai_code_change_agent.application.changes.ports import ChangeUnitOfWork
from ai_code_change_agent.domain.changes.errors import (
    ChangeConflictError,
    ConcurrentModificationError,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus as S
from tests.support.changes import MemoryChanges, MemoryChangeUow


class LosingRaces:
    """A unit-of-work factory whose first `losses` commits lose an optimistic race."""

    def __init__(self, changes: MemoryChanges, losses: int) -> None:
        self.changes = changes
        self.losses = losses
        self.opened = 0

    def __call__(self) -> ChangeUnitOfWork:
        self.opened += 1
        if not self.losses:
            return self.changes.uow()
        self.losses -= 1
        # the memory store writes through: a lost unit of work works on a copy that is discarded
        uow = MemoryChangeUow(copy.deepcopy(self.changes.state))

        async def lost() -> None:
            raise ConcurrentModificationError()

        uow.commit = lost  # type: ignore[method-assign]
        return uow


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def assigned(changes: MemoryChanges) -> uuid.UUID:
    return changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4()).id


async def test_a_lost_race_reruns_start_attempt_in_a_new_unit_of_work() -> None:
    changes, sleeps = MemoryChanges(), Sleeps()
    rid = assigned(changes)
    factory = LosingRaces(changes, losses=2)
    svc = changes.service()
    svc._uow, svc._sleep = factory, sleeps

    attempt = await svc.start_attempt(rid)

    assert factory.opened == 3 and len(sleeps.delays) == 2
    assert attempt.number == 1


async def test_every_mutation_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "random", lambda: 1.0)
    changes, sleeps = MemoryChanges(), Sleeps()
    rid = assigned(changes)
    svc = changes.service(sleep=sleeps)
    attempt = await svc.start_attempt(rid)
    factory = LosingRaces(changes, losses=1)
    svc._uow = factory

    moved = await svc.transition(attempt.id, S.PREPARING)

    assert moved.status is S.PREPARING and factory.opened == 2
    assert sleeps.delays == [0.025]


async def test_retries_are_bounded_and_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "random", lambda: 1.0)
    changes, sleeps = MemoryChanges(), Sleeps()
    rid = assigned(changes)
    factory = LosingRaces(changes, losses=99)
    svc = changes.service(sleep=sleeps)
    svc._uow = factory

    with pytest.raises(ConcurrentModificationError):
        await svc.start_attempt(rid)

    assert factory.opened == retry_module.RETRY_ATTEMPTS == 5
    assert sleeps.delays == [0.025, 0.05, 0.1, 0.2]


async def test_genuine_conflicts_are_not_retried() -> None:
    changes, sleeps = MemoryChanges(), Sleeps()
    rid = assigned(changes)
    svc = changes.service(sleep=sleeps)
    attempt = await svc.start_attempt(rid)
    opened = 0

    def conflicting() -> ChangeUnitOfWork:
        nonlocal opened
        opened += 1
        uow = changes.uow()

        async def conflict() -> None:
            raise ChangeConflictError("attempt_changed_concurrently")

        uow.commit = conflict  # type: ignore[method-assign]
        return uow

    svc._uow = conflicting
    with pytest.raises(ChangeConflictError) as raised:
        await svc.transition(attempt.id, S.PREPARING)

    assert not isinstance(raised.value, ConcurrentModificationError)
    assert opened == 1 and sleeps.delays == []
