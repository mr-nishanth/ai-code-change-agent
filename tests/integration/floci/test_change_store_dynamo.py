"""The DynamoDB change adapter against Floci (DYNAMODB-ACCESS-PATTERNS.md 2, C1-C11).

Scenarios ported from the change part of `tests/integration/test_jobs_and_changes_db.py` (which
stays until PostgreSQL is removed), plus the races the row locks used to serialise. Each test runs
on its own `s2116-<uuid>` table through `CodeChangeService` wired to `DynamoChangeUnitOfWork`;
requests are seeded with `request_item`, as intake will write them."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.domain.changes.errors import (
    ChangeConflictError,
    ConcurrentModificationError,
    ImmutableAssignmentError,
    InvalidTransitionError,
)
from ai_code_change_agent.domain.changes.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
    CodeChangeRequest,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus as S
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.change_store import (
    AttemptPointers,
    DynamoChangeUnitOfWork,
    request_item,
)

pytestmark = [pytest.mark.floci, pytest.mark.integration]
PLAN = "a" * 64
BASE = "c" * 40


def uow(table: Table, pointers: AttemptPointers | None = None) -> DynamoChangeUnitOfWork:
    return DynamoChangeUnitOfWork(table, pointers=pointers or AttemptPointers())


def service(table: Table) -> CodeChangeService:
    return CodeChangeService(lambda: uow(table), max_repairs=1)


async def seed_request(table: Table, *, repository_id: uuid.UUID | None = None) -> uuid.UUID:
    version = uuid.uuid4() if repository_id is not None else None
    request = CodeChangeRequest(uuid.uuid4(), "10001", "SHOP-1", "SHOP", repository_id, version)
    await table.put(request_item(request, created_at=datetime.now(UTC)))
    return request.id


async def attempt_at_plan_approval(table: Table) -> tuple[CodeChangeService, uuid.UUID]:
    svc = service(table)
    rid = await seed_request(table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    await svc.transition(aid, S.PREPARING)
    await svc.pin_baseline(aid, BASE)
    await svc.transition(aid, S.PLANNING)
    await svc.record_plan(aid, "art_p", PLAN)
    await svc.transition(aid, S.AWAITING_PLAN_APPROVAL)
    return svc, aid


async def collection(table: Table, rid: uuid.UUID, prefix: str) -> list[dict[str, object]]:
    return await table.query(
        key_condition="PK = :pk AND begins_with(SK, :p)",
        values={":pk": keys.request_pk(rid), ":p": prefix},
    )


def calls(table: Table, operation: str) -> list[str]:
    seen: list[str] = []
    table._client.meta.events.register(
        f"before-call.dynamodb.{operation}", lambda **_: seen.append(operation)
    )
    return seen


async def test_the_store_round_trips_the_lifecycle(app_table: Table) -> None:
    svc, aid = await attempt_at_plan_approval(app_table)
    approval, _ = await svc.decide(
        aid, ApprovalGate.PLAN, PLAN, ApprovalDecision.APPROVED, "cli:op"
    )
    assert [a.id for a in await svc.approvals(aid)] == [approval.id]
    attempt = await svc.transition(aid, S.IMPLEMENTING)
    assert attempt.status is S.IMPLEMENTING and attempt.base_commit_sha == BASE
    assert attempt.plan_sha256 == PLAN and attempt.row_version == 7

    stored = await svc.get_attempt(aid)
    assert stored == attempt
    request = await svc.get_request(attempt.request_id)
    assert request.status == "IMPLEMENTING" and request.current_attempt_id == aid
    (guard,) = await collection(app_table, attempt.request_id, keys.SK_ACTIVE)
    assert guard["attempt_id"] == str(aid)


async def test_assignment_of_another_repository_is_refused(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table)
    repo, version = uuid.uuid4(), uuid.uuid4()
    assigned = await svc.assign_repository(rid, repo, version)
    assert assigned.repository_id == repo and assigned.profile_version_id == version
    assert await svc.assign_repository(rid, repo, version) == assigned
    with pytest.raises(ImmutableAssignmentError, match=r"^repository_already_assigned$"):
        await svc.assign_repository(rid, uuid.uuid4(), version)
    attempt = await svc.start_attempt(rid)
    assert (attempt.repository_id, attempt.profile_version_id) == (repo, version)


async def test_eight_concurrent_starts_create_one_attempt(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())

    started = await asyncio.gather(*(svc.start_attempt(rid) for _ in range(8)))

    assert len({a.id for a in started}) == 1 and started[0].number == 1
    async with uow(app_table) as u:
        assert [a.id for a in await u.store.list_attempts(rid)] == [started[0].id]
    assert len(await collection(app_table, rid, keys.SK_ACTIVE)) == 1


async def test_two_repositories_get_numbers_one_and_two(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table)  # multi-repository: no repository on the request
    repos = [uuid.uuid4(), uuid.uuid4()]

    started = await asyncio.gather(
        *(svc.start_repository_attempt(rid, repo, uuid.uuid4()) for repo in repos)
    )

    assert sorted(a.number for a in started) == [1, 2]
    assert {a.repository_id for a in started} == set(repos)
    again = await svc.start_repository_attempt(rid, repos[0], uuid.uuid4())
    assert again.id == next(a.id for a in started if a.repository_id == repos[0])
    assert len(await collection(app_table, rid, keys.SK_ACTIVE)) == 2


async def test_a_terminal_attempt_releases_its_guard_and_the_next_one_is_number_two(
    app_table: Table,
) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    first = await svc.start_attempt(rid)
    cancelled = await svc.transition(first.id, S.CANCELLED)
    assert await collection(app_table, rid, keys.SK_ACTIVE) == []

    second = await svc.start_attempt(rid)

    assert second.number == 2 and second.id != first.id
    (guard,) = await collection(app_table, rid, keys.SK_ACTIVE)
    assert guard["attempt_id"] == str(second.id)
    assert (await svc.get_attempt(first.id)) == cancelled  # attempts are never deleted
    assert (await svc.get_request(rid)).current_attempt_id == second.id


async def test_update_of_a_terminal_attempt_is_refused(app_table: Table) -> None:
    svc, aid = await attempt_at_plan_approval(app_table)
    _, rejected = await svc.decide(
        aid, ApprovalGate.PLAN, PLAN, ApprovalDecision.REJECTED, "cli:op"
    )
    assert rejected.status is S.REJECTED

    with pytest.raises(InvalidTransitionError):  # the domain refuses first
        await svc.transition(aid, S.IMPLEMENTING)
    async with uow(app_table) as u:  # the adapter refuses a direct write too
        current = await u.store.lock_attempt(aid)
        assert current is not None
        changed = CodeChangeAttempt(**{**current.__dict__, "failure_code": "x"})
        with pytest.raises(ChangeConflictError, match=r"^attempt_immutable$"):
            await u.store.update_attempt(changed, expected_version=current.row_version)
    assert await svc.get_attempt(aid) == rejected


async def test_the_condition_refuses_a_write_to_an_attempt_that_became_terminal(
    app_table: Table,
) -> None:
    """The commit condition, not only the in-memory check: the stored item turns terminal (same
    version) between the read and the commit."""
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    async with uow(app_table) as u:
        current = await u.store.lock_attempt(aid)
        assert current is not None
        key = keys.attempt(rid, aid)
        await app_table.update(
            key, update="SET #s = :s", names={"#s": "status"}, values={":s": "CANCELLED"}
        )
        await u.store.update_attempt(current.transition(S.PREPARING), expected_version=1)
        with pytest.raises(ChangeConflictError, match=r"^attempt_immutable$") as raised:
            await u.commit()
    assert not isinstance(raised.value, ConcurrentModificationError)


async def test_a_base_commit_change_is_refused(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    await svc.transition(aid, S.PREPARING)
    pinned = await svc.pin_baseline(aid, BASE)

    async with uow(app_table) as u:
        other = CodeChangeAttempt(**{**pinned.__dict__, "base_commit_sha": "d" * 40})
        with pytest.raises(ChangeConflictError, match=r"^baseline_immutable$"):
            await u.store.update_attempt(other, expected_version=pinned.row_version)
        cleared = CodeChangeAttempt(**{**pinned.__dict__, "base_commit_sha": None})
        with pytest.raises(ChangeConflictError, match=r"^baseline_immutable$"):
            await u.store.update_attempt(cleared, expected_version=pinned.row_version)

    # the commit condition: the stored base changes (same version) between read and commit
    async with uow(app_table) as u:
        current = await u.store.lock_attempt(aid)
        assert current is not None
        await app_table.update(
            keys.attempt(rid, aid), update="SET base_commit_sha = :b", values={":b": "e" * 40}
        )
        await u.store.update_attempt(current.transition(S.PLANNING), expected_version=3)
        with pytest.raises(ChangeConflictError, match=r"^baseline_immutable$"):
            await u.commit()
    assert (await svc.pin_baseline(aid, "e" * 40)).base_commit_sha == "e" * 40


async def test_a_stale_caller_gets_attempt_changed_concurrently(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    started = await svc.start_attempt(rid)
    await svc.transition(started.id, S.PREPARING)
    async with uow(app_table) as u:
        with pytest.raises(ChangeConflictError, match=r"^attempt_changed_concurrently$") as raised:
            await u.store.update_attempt(
                started.transition(S.FAILED, failure_code="x"), expected_version=started.row_version
            )
    assert not isinstance(raised.value, ConcurrentModificationError)


async def test_a_lost_race_on_the_attempt_is_a_concurrent_modification(app_table: Table) -> None:
    svc = service(app_table)
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    async with uow(app_table) as u:
        current = await u.store.lock_attempt(aid)
        assert current is not None
        await svc.transition(aid, S.PREPARING)  # another worker wins
        await u.store.update_attempt(current.transition(S.CANCELLED), expected_version=1)
        with pytest.raises(ConcurrentModificationError):
            await u.commit()
    assert (await svc.get_attempt(aid)).status is S.PREPARING
    assert len(await collection(app_table, rid, keys.SK_ACTIVE)) == 1


@pytest.mark.parametrize("cancel_first", [False, True])
@pytest.mark.parametrize("decision", [ApprovalDecision.REJECTED, ApprovalDecision.APPROVED])
async def test_decide_racing_transition_stays_consistent(
    app_table: Table, decision: ApprovalDecision, cancel_first: bool
) -> None:
    svc, aid = await attempt_at_plan_approval(app_table)
    decide = svc.decide(aid, ApprovalGate.PLAN, PLAN, decision, "cli:op")
    cancel = svc.transition(aid, S.CANCELLED)

    if cancel_first:
        cancelled, decided = await asyncio.gather(cancel, decide, return_exceptions=True)
    else:
        decided, cancelled = await asyncio.gather(decide, cancel, return_exceptions=True)

    final = await svc.get_attempt(aid)
    approvals = await svc.approvals(aid)
    request = await svc.get_request(final.request_id)
    assert request.status == final.status.value
    if isinstance(decided, BaseException):  # the cancel won; the decision saw a terminal attempt
        assert isinstance(decided, InvalidTransitionError), decided
        assert final.status is S.CANCELLED and approvals == []
    elif isinstance(cancelled, BaseException):  # the decision won (a rejection ends the attempt)
        assert decision is ApprovalDecision.REJECTED
        assert isinstance(cancelled, InvalidTransitionError), cancelled
        assert final.status is S.REJECTED and [a.decision for a in approvals] == [decision]
    else:  # an approval and then the cancel: both apply, in that order
        assert decision is ApprovalDecision.APPROVED
        assert final.status is S.CANCELLED and [a.decision for a in approvals] == [decision]
    assert final.row_version in (6, 7)
    assert await collection(app_table, final.request_id, keys.SK_ACTIVE) == []


async def test_approvals_are_append_only_and_ordered_by_decided_at(app_table: Table) -> None:
    svc, aid = await attempt_at_plan_approval(app_table)
    start = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
    order = [3, 0, 2, 1]
    made = [
        Approval(
            aid,
            ApprovalGate.PLAN,
            PLAN,
            ApprovalDecision.APPROVED,
            "cli:op",
            decided_at=start + timedelta(seconds=n),
        )
        for n in order
    ]
    async with uow(app_table) as u:
        for approval in made:
            await u.store.add_approval(approval)
        await u.commit()
    async with uow(app_table) as u:
        with pytest.raises(ChangeConflictError, match=r"^approval_exists$"):
            await u.store.add_approval(made[0])
            await u.commit()

    listed = await svc.approvals(aid)

    assert [a.decided_at for a in listed] == sorted(a.decided_at for a in made)
    assert {a.id for a in listed} == {a.id for a in made}
    assert all(a.actor_verified is False for a in listed)
    assert await svc.approvals(uuid.uuid4()) == []


async def test_the_attempt_pointer_is_cached_after_commit_only(app_table: Table) -> None:
    pointers = AttemptPointers()
    svc = CodeChangeService(lambda: uow(app_table, pointers))
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    gets = calls(app_table, "GetItem")

    await svc.get_attempt(aid)
    first = len(gets)
    await svc.get_attempt(aid)

    assert first == 2 and len(gets) == 3  # pointer + attempt, then the attempt only
    assert pointers.get(app_table.name, aid) == rid
    assert await svc.approvals(aid) == []
    async with uow(app_table, pointers) as u:  # an uncommitted pointer is never cached
        new = CodeChangeAttempt(uuid.uuid4(), rid, 9, uuid.uuid4(), uuid.uuid4())
        await u.store.add_attempt(new)
        assert await u.store.get_attempt(new.id) is not None
    assert pointers.get(app_table.name, new.id) is None
    async with uow(app_table, pointers) as u:
        assert await u.store.get_attempt(new.id) is None
        assert await u.store.get_request(uuid.uuid4()) is None


def test_the_pointer_cache_is_bounded() -> None:
    pointers = AttemptPointers(max_entries=2)
    ids = [uuid.uuid4() for _ in range(3)]
    for aid in ids:
        pointers.put("t", aid, aid)
    assert pointers.get("t", ids[0]) is None
    assert pointers.get("t", ids[2]) == ids[2]
