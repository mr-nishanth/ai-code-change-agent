"""Code change use cases over the in-memory unit of work."""

import uuid

import pytest

from ai_code_change_agent.domain.changes.errors import (
    ActiveAttemptError,
    ChangeConflictError,
    ChangeNotFoundError,
    ImmutableAssignmentError,
    StaleApprovalError,
)
from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus
from tests.support.changes import MemoryChanges

S = AttemptStatus
A = "a" * 64


async def test_assignment_is_set_once_and_attempts_are_idempotent() -> None:
    m = MemoryChanges()
    svc = m.service()
    req = m.add_request()
    with pytest.raises(ActiveAttemptError):
        await svc.start_attempt(req.id)
    repo, ver = uuid.uuid4(), uuid.uuid4()
    await svc.assign_repository(req.id, repo, ver)
    assert (await svc.assign_repository(req.id, repo, ver)).repository_id == repo
    with pytest.raises(ImmutableAssignmentError):
        await svc.assign_repository(req.id, uuid.uuid4(), ver)
    first = await svc.start_attempt(req.id)
    again = await svc.start_attempt(req.id)  # a re-delivered intake job
    assert first.id == again.id and first.number == 1
    assert (await svc.get_request(req.id)).current_attempt_id == first.id
    assert (await svc.get_request(req.id)).status == "PENDING"


async def test_transitions_project_onto_the_request() -> None:
    m = MemoryChanges()
    svc = m.service()
    req = m.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    att = await svc.start_attempt(req.id)
    await svc.transition(att.id, S.PREPARING)
    await svc.pin_baseline(att.id, "c" * 40)
    await svc.transition(att.id, S.PLANNING)
    await svc.record_plan(att.id, "art_p", A)
    await svc.transition(att.id, S.AWAITING_PLAN_APPROVAL)
    assert (await svc.get_request(req.id)).status == "AWAITING_PLAN_APPROVAL"
    with pytest.raises(StaleApprovalError):
        await svc.decide(att.id, ApprovalGate.PLAN, "b" * 64, ApprovalDecision.APPROVED, "cli:op")
    approval, current = await svc.decide(
        att.id, ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, "cli:op"
    )
    assert current.status is S.AWAITING_PLAN_APPROVAL  # the workflow moves it, not the decision
    assert await svc.approvals(att.id) == [approval]
    assert (await svc.transition(att.id, S.IMPLEMENTING)).status is S.IMPLEMENTING


async def test_a_rejection_ends_the_attempt_and_allows_a_new_one() -> None:
    m = MemoryChanges()
    svc = m.service()
    req = m.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    att = await svc.start_attempt(req.id)
    m.force(att.id, status=S.AWAITING_PLAN_APPROVAL, plan_sha256=A)
    _, rejected = await svc.decide(
        att.id, ApprovalGate.PLAN, A, ApprovalDecision.REJECTED, "cli:op"
    )
    assert rejected.status is S.REJECTED
    assert (await svc.get_request(req.id)).status == "REJECTED"
    assert (await svc.start_attempt(req.id)).number == 2


async def test_unknown_ids_and_lost_updates_are_refused() -> None:
    m = MemoryChanges()
    svc = m.service()
    for call in (
        svc.get_request(uuid.uuid4()),
        svc.get_attempt(uuid.uuid4()),
        svc.start_attempt(uuid.uuid4()),
        svc.transition(uuid.uuid4(), S.FAILED),
        svc.assign_repository(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()),
        svc.decide(uuid.uuid4(), ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, "cli:o"),
    ):
        with pytest.raises(ChangeNotFoundError):
            await call
    req = m.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    att = await svc.start_attempt(req.id)
    stale = await svc.get_attempt(att.id)
    await svc.transition(att.id, S.PREPARING)
    with pytest.raises(ChangeConflictError):
        await m.uow().store.update_attempt(stale, expected_version=stale.row_version)


async def test_studio_reads_listings_and_agent_threads() -> None:
    m = MemoryChanges()
    svc = m.service()
    req = m.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    att = await svc.start_attempt(req.id)
    assert [x.attempt.id for x in await svc.recent_attempts(5)] == [att.id]
    assert (await svc.attempt_listing(att.id)).attempt == att
    with pytest.raises(ChangeNotFoundError):
        await svc.attempt_listing(uuid.uuid4())
    thread = f"attempt:{att.id}:planning:00000000"
    await svc.record_agent_thread(att.id, thread)
    await svc.record_agent_thread(att.id, thread)
    assert await svc.agent_threads(att.id) == [thread]
    with pytest.raises(ValueError, match="does not belong"):
        await svc.record_agent_thread(att.id, "attempt:other:planning:00000000")
