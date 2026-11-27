"""PostgreSQL: job queue claiming, leases and recovery; change store and database guarantees."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.jobs.queue import Job, JobStatus
from ai_code_change_agent.application.jobs.worker import Worker
from ai_code_change_agent.composition import build_registry_service
from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.persistence.repositories.change_store import SqlChangeUnitOfWork
from ai_code_change_agent.persistence.repositories.job_queue import PostgresJobQueue
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq, query
from tests.support.changes import python_profile
from tests.support.registry_fixtures import new_repo

pytestmark = pytest.mark.integration
S = AttemptStatus


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def sql(url: str, statement: str, params: tuple[object, ...] = ()) -> None:
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        conn.execute(statement, params)


# ---- job queue ---------------------------------------------------------------------------
async def test_enqueue_is_deduplicated(sessions: async_sessionmaker[AsyncSession]) -> None:
    q = PostgresJobQueue(sessions)
    assert await q.enqueue("code_change.intake", {"a": 1}, dedupe_key="k1", max_attempts=3)
    assert not await q.enqueue("code_change.intake", {"a": 2}, dedupe_key="k1", max_attempts=3)


async def test_concurrent_workers_never_claim_the_same_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    q = PostgresJobQueue(sessions)
    for i in range(5):
        await q.enqueue("k.test", {"i": i}, dedupe_key=f"d{i}", max_attempts=3)
    claims = await asyncio.gather(*[q.claim(f"w{i}", 60) for i in range(12)])
    got = [c for c in claims if c is not None]
    assert len(got) == 5 and len({c.id for c in got}) == 5 and all(c.attempts == 1 for c in got)


async def test_lease_heartbeat_completion_and_owner_checks(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    job = await q.claim("w1", 60)
    assert job is not None
    assert await q.heartbeat(job.id, "w1", 60) and not await q.heartbeat(job.id, "w2", 60)
    assert not await q.complete(job.id, "w2")
    assert await q.complete(job.id, "w1")
    assert query(database_url, "SELECT status, lease_owner FROM jobs") == [("SUCCEEDED", None)]
    assert await q.claim("w1", 60) is None


async def test_a_crashed_workers_job_is_reclaimed_after_the_lease_expires(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    first = await q.claim("crashed", 60)
    assert first is not None and await q.claim("w2", 60) is None  # lease still valid
    sql(database_url, "UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
    again = await q.claim("w2", 60)
    assert again is not None and again.id == first.id and again.attempts == 2
    assert not await q.heartbeat(first.id, "crashed", 60)  # the old owner lost the lease


async def test_retry_backoff_then_dead_when_attempts_are_exhausted(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=2)
    job = await q.claim("w", 60)
    assert job is not None
    assert await q.fail(job.id, "w", "temporary", retry_in_seconds=3600) is JobStatus.QUEUED
    assert await q.claim("w", 60) is None  # not due yet
    sql(database_url, "UPDATE jobs SET available_at = now()")
    job = await q.claim("w", 60)
    assert job is not None and job.attempts == 2
    assert await q.fail(job.id, "w", "temporary", retry_in_seconds=1) is JobStatus.DEAD
    assert query(database_url, "SELECT status, last_error FROM jobs") == [("DEAD", "temporary")]


async def test_an_expired_job_without_attempts_left_is_reaped(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=1)
    assert await q.claim("crashed", 60) is not None
    sql(database_url, "UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
    assert await q.claim("w2", 60) is None
    assert query(database_url, "SELECT status, last_error FROM jobs") == [
        ("DEAD", "lease_expired_attempts_exhausted")
    ]


async def test_an_expired_lease_is_reclaimed_by_one_worker_and_a_second_expiry_is_dead(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=2)
    first = await q.claim("crashed-1", 60)
    assert first is not None and first.attempts == 1
    sql(database_url, "UPDATE jobs SET lease_expires_at = now() - interval '1 second'")

    claims = await asyncio.gather(*[q.claim(f"w{i}", 60) for i in range(8)])
    (winner,) = [c for c in claims if c is not None]  # exactly one reclaims it
    assert winner.id == first.id and winner.attempts == 2
    owner = f"w{claims.index(winner)}"
    assert query(database_url, "SELECT status, attempts, lease_owner FROM jobs") == [
        ("RUNNING", 2, owner)
    ]  # the retry count moved once, not once per competing worker

    # the second worker crashes too: its last attempt is used, so the job is DEAD, not retried
    sql(database_url, "UPDATE jobs SET lease_expires_at = now() - interval '1 second'")
    assert await q.claim("w-late", 60) is None
    assert query(database_url, "SELECT status, attempts, last_error, lease_owner FROM jobs") == [
        ("DEAD", 2, "lease_expired_attempts_exhausted", None)
    ]
    assert not await q.complete(winner.id, owner)  # a late finish cannot revive it
    assert await q.claim("w-later", 60) is None


async def test_a_permanent_failure_is_dead_and_fail_requires_the_owner(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    job = await q.claim("w", 60)
    assert job is not None
    assert await q.fail(job.id, "other", "x", retry_in_seconds=None) is JobStatus.RUNNING
    assert await q.fail(job.id, "w", "bad_input", retry_in_seconds=None) is JobStatus.DEAD


async def test_the_worker_runs_against_postgres(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    q = PostgresJobQueue(sessions)
    await q.enqueue("k.test", {"x": 1}, dedupe_key="d", max_attempts=3)
    seen: list[Job] = []

    async def handler(job: Job) -> None:
        seen.append(job)

    w = Worker(q, {"k.test": handler}, lease_seconds=30, heartbeat_seconds=10)
    assert await w.run_once() is not None
    assert seen[0].payload == {"x": 1}
    assert query(database_url, "SELECT status FROM jobs") == [("SUCCEEDED",)]


# ---- change store and database guarantees -------------------------------------------------
async def _attempt(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[CodeChangeService, uuid.UUID]:
    reg = build_registry_service(sessions)
    await reg.register_repository(new_repo("shop-service", pid="424242"), actor="cli:t")
    await reg.create_version("shop-service", python_profile(), actor="cli:t")
    await reg.publish_version("shop-service", 1, actor="cli:t")
    repo, versions, _, _ = await reg.show_repository("shop-service")
    request_id = uuid.uuid4()
    async with sessions() as s, s.begin():
        from ai_code_change_agent.persistence.models.webhooks import CodeChangeRequest

        s.add(
            CodeChangeRequest(
                id=request_id,
                jira_issue_id="10001",
                jira_issue_key="SHOP-1",
                jira_project_key="SHOP",
            )
        )
    svc = CodeChangeService(lambda: SqlChangeUnitOfWork(sessions), max_repairs=1)
    await svc.assign_repository(request_id, repo.id, versions[0].id)
    attempt = await svc.start_attempt(request_id)
    return svc, attempt.id


async def test_the_sql_store_round_trips_the_lifecycle(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc, aid = await _attempt(sessions)
    await svc.transition(aid, S.PREPARING)
    await svc.pin_baseline(aid, "c" * 40)
    await svc.transition(aid, S.PLANNING)
    await svc.record_plan(aid, "art_p", "a" * 64)
    await svc.transition(aid, S.AWAITING_PLAN_APPROVAL)
    approval, _ = await svc.decide(
        aid, ApprovalGate.PLAN, "a" * 64, ApprovalDecision.APPROVED, "cli:op"
    )
    assert (await svc.approvals(aid))[0].id == approval.id
    attempt = await svc.transition(aid, S.IMPLEMENTING)
    assert attempt.status is S.IMPLEMENTING and attempt.base_commit_sha == "c" * 40
    assert query(database_url, "SELECT status FROM code_change_requests") == [("IMPLEMENTING",)]


async def test_the_sql_store_lists_attempts_but_keeps_no_thread_index(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    svc, aid = await _attempt(sessions)
    (listed,) = await svc.recent_attempts(10)
    assert listed.attempt.id == aid and listed.created_at <= listed.updated_at
    assert (await svc.attempt_listing(aid)).attempt.id == aid
    await svc.record_agent_thread(aid, f"attempt:{aid}:planning:00000000")
    assert await svc.agent_threads(aid) == []  # studio reads only the DynamoDB index


async def test_database_guarantees_hold_even_for_direct_sql(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc, aid = await _attempt(sessions)
    await svc.transition(aid, S.PREPARING)
    await svc.pin_baseline(aid, "c" * 40)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        sql(database_url, "UPDATE code_change_attempts SET base_commit_sha = %s", ("d" * 40,))
    await svc.transition(aid, S.PLANNING)
    await svc.record_plan(aid, "art_p", "a" * 64)
    await svc.transition(aid, S.AWAITING_PLAN_APPROVAL)
    await svc.decide(aid, ApprovalGate.PLAN, "a" * 64, ApprovalDecision.APPROVED, "cli:op")
    for statement in (
        "UPDATE code_change_approvals SET decision = 'REJECTED'",
        "DELETE FROM code_change_approvals",
        "DELETE FROM code_change_attempts",
    ):
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            sql(database_url, statement)
    request_id = query(database_url, "SELECT request_id FROM code_change_attempts")[0][0]
    with pytest.raises(psycopg.errors.UniqueViolation):  # one active attempt per request
        sql(
            database_url,
            "INSERT INTO code_change_attempts (id, request_id, number, repository_id, "
            "profile_version_id, status) SELECT %s, request_id, 2, repository_id, "
            "profile_version_id, 'PENDING' FROM code_change_attempts",
            (uuid.uuid4(),),
        )
    assert request_id is not None
    await svc.decide(aid, ApprovalGate.PLAN, "a" * 64, ApprovalDecision.REJECTED, "cli:op")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):  # terminal is immutable
        sql(database_url, "UPDATE code_change_attempts SET failure_code = 'x'")
