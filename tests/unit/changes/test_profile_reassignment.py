"""A request may move to a newer profile of the same repository only between attempts."""

import uuid

import pytest

from ai_code_change_agent.domain.changes.errors import ImmutableAssignmentError
from ai_code_change_agent.domain.changes.states import AttemptStatus
from tests.support.changes import MemoryChanges


async def test_a_newer_profile_replaces_a_stale_one_only_when_no_attempt_is_active() -> None:
    changes = MemoryChanges()
    repo, v1, v2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    request = changes.add_request(repository_id=repo, profile_version_id=v1)
    svc = changes.service()
    attempt = await svc.start_attempt(request.id)
    with pytest.raises(ImmutableAssignmentError):  # an active attempt keeps its profile
        await svc.assign_repository(request.id, repo, v2)
    await svc.transition(attempt.id, AttemptStatus.FAILED, failure_code="x")
    updated = await svc.assign_repository(request.id, repo, v2)
    assert updated.profile_version_id == v2
    fresh = await svc.start_attempt(request.id)
    assert fresh.profile_version_id == v2 and fresh.number == 2
    with pytest.raises(ImmutableAssignmentError):  # another repository is never accepted
        await svc.assign_repository(request.id, uuid.uuid4(), v2)
    assert (await svc.assign_repository(request.id, repo, v2)).profile_version_id == v2
