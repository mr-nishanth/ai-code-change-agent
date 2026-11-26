"""PostgreSQL: the durable Jira reporting lifecycle of a multi-repository parent."""

import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.multirepo.models import Parent, ParentStatus, ReportState
from ai_code_change_agent.persistence.repositories.multirepo_store import PostgresMultiRepoStore
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq, query

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


async def finished_parent(url: str, store: PostgresMultiRepoStore) -> uuid.UUID:
    request_id = uuid.uuid4()
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO code_change_requests (id, jira_issue_id, jira_issue_key, "
            "jira_project_key, status) VALUES (%s, %s, 'SHOP-9', 'SHOP', 'COMPLETED')",
            (request_id, str(uuid.uuid4().int)[:18]),
        )
    parent = Parent(uuid.uuid4(), request_id, ParentStatus.COMPLETED, {"repositories": []})
    await store.create(parent, [])
    return parent.id


async def test_report_lifecycle_persists_and_is_claimed_once(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    store = PostgresMultiRepoStore(sessions)
    pid = await finished_parent(database_url, store)
    claim = await store.claim_report(pid, stale_seconds=300)
    assert claim is not None and claim.previous is ReportState.PENDING and claim.attempts == 1
    assert await store.claim_report(pid, stale_seconds=300) is None  # in flight: nobody else
    await store.finish_report(pid, ReportState.RETRYABLE_FAILURE, error="http_503")
    retry = await store.claim_report(pid, stale_seconds=300)
    assert retry is not None and retry.previous is ReportState.RETRYABLE_FAILURE
    assert retry.attempts == 2
    await store.finish_report(pid, ReportState.SUCCEEDED, comment_id="10009")
    parent, _ = await PostgresMultiRepoStore(sessions).load(pid)  # "after a restart"
    assert parent.jira_report == "SUCCEEDED" and parent.jira_comment_id == "10009"
    assert parent.jira_report_attempts == 2 and parent.jira_report_error is None
    assert parent.status is ParentStatus.COMPLETED  # execution status is separate
    assert await store.claim_report(pid, stale_seconds=300) is None  # never posted twice


async def test_a_crashed_in_progress_claim_is_recovered_only_when_stale(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    store = PostgresMultiRepoStore(sessions)
    pid = await finished_parent(database_url, store)
    assert await store.claim_report(pid, stale_seconds=300) is not None  # worker dies here
    assert await store.claim_report(pid, stale_seconds=300) is None
    query(
        database_url,
        "UPDATE code_change_parents SET jira_report_at = now() - interval '10 minutes' "
        "WHERE id = %s RETURNING id",
        (pid,),
    )
    recovered = await store.claim_report(pid, stale_seconds=300)
    assert recovered is not None and recovered.previous is ReportState.IN_PROGRESS


async def test_a_new_report_round_resets_the_report_but_keeps_history_in_jira(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    store = PostgresMultiRepoStore(sessions)
    pid = await finished_parent(database_url, store)
    await store.claim_report(pid, stale_seconds=300)
    await store.finish_report(pid, ReportState.SUCCEEDED, comment_id="10001")
    assert await store.update_parent(
        pid, frozenset({ParentStatus.COMPLETED}), status=ParentStatus.RUNNING, reopen=True
    )
    parent, _ = await store.load(pid)
    assert parent.report_round == 1 and parent.jira_report is None
    assert parent.jira_comment_id is None and parent.jira_report_attempts == 0


async def test_the_database_rejects_unknown_report_states(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    pid = await finished_parent(database_url, PostgresMultiRepoStore(sessions))
    with (
        psycopg.connect(libpq(database_url), autocommit=True) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        conn.execute("UPDATE code_change_parents SET jira_report = 'POSTED' WHERE id = %s", (pid,))
