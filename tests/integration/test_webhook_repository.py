import asyncio
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_code_change_agent.domain.webhooks.models import (
    IgnoreReason,
    IssueRef,
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.persistence.repositories.webhook_repository import (
    SqlAlchemyWebhookRepository,
)
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq, query

pytestmark = pytest.mark.integration


def issue(issue_id: str = "10001") -> IssueRef:
    return IssueRef(
        issue_id=issue_id, issue_key=f"TST-{issue_id}", project_key="TST", issue_type="Story"
    )


def accepted(delivery_id: str = "d-1", issue_id: str = "10001") -> ReceivedDelivery:
    return ReceivedDelivery(
        delivery_id=delivery_id,
        payload_sha256="a" * 64,
        event_type="jira:issue_created",
        retry_count=0,
        flow="Primary",
        status=ProcessingStatus.ACCEPTED,
        ignore_reason=None,
        issue=issue(issue_id),
    )


def ignored(delivery_id: str = "d-ign") -> ReceivedDelivery:
    return ReceivedDelivery(
        delivery_id=delivery_id,
        payload_sha256="b" * 64,
        event_type="jira:issue_updated",
        retry_count=1,
        flow=None,
        status=ProcessingStatus.IGNORED,
        ignore_reason=IgnoreReason.EVENT_NOT_SUPPORTED,
        issue=None,
    )


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def repo(engine: AsyncEngine) -> SqlAlchemyWebhookRepository:
    return SqlAlchemyWebhookRepository(create_session_factory(engine))


def counts(url: str) -> tuple[int, int, int]:
    d = query(url, "SELECT count(*) FROM jira_deliveries")[0][0]
    m = query(url, "SELECT count(*) FROM code_change_requests")[0][0]
    o = query(url, "SELECT count(*) FROM jobs")[0][0]
    return int(d), int(m), int(o)  # type: ignore[call-overload]


async def test_accepted_writes_delivery_code_change_request_and_intake_job_together(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    assert await repo.record(accepted()) is Outcome.ACCEPTED
    assert counts(database_url) == (1, 1, 1)
    mig = query(
        database_url,
        "SELECT id::text, status, jira_project_key, jira_issue_id, jira_issue_key FROM code_change_requests",
    )[0]
    assert mig[1] == "RECEIVED" and mig[3] == "10001"
    d = query(
        database_url,
        "SELECT code_change_request_id::text, processing_result, verification_result, flow, retry_count FROM jira_deliveries",
    )[0]
    assert d == (mig[0], "ACCEPTED", "VERIFIED", "Primary", 0)
    job = query(
        database_url,
        "SELECT kind, status, attempts, max_attempts, payload::text, dedupe_key FROM jobs",
    )[0]
    assert job[:4] == ("code_change.intake", "QUEUED", 0, 3)
    assert job[5] == f"code_change.intake:{mig[0]}"
    assert set(__import__("json").loads(str(job[4]))) == {
        "code_change_request_id",
        "jira_issue_id",
        "jira_issue_key",
    }


async def test_ignored_writes_only_delivery(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    assert await repo.record(ignored()) is Outcome.IGNORED
    assert counts(database_url) == (1, 0, 0)
    row = query(
        database_url, "SELECT processing_result, ignore_reason, jira_issue_id FROM jira_deliveries"
    )[0]
    assert row == ("IGNORED", "event_not_supported", None)


async def test_duplicate_delivery_id_changes_nothing(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    await repo.record(accepted())
    assert await repo.record(accepted()) is Outcome.DUPLICATE
    assert await repo.record(ignored("d-1")) is Outcome.DUPLICATE
    assert counts(database_url) == (1, 1, 1)


async def test_new_delivery_for_same_issue_creates_no_second_code_change_request(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    await repo.record(accepted("d-1"))
    assert await repo.record(accepted("d-2")) is Outcome.DUPLICATE
    assert counts(database_url) == (2, 1, 1)
    res = query(
        database_url,
        "SELECT delivery_id, processing_result, code_change_request_id IS NOT NULL FROM jira_deliveries ORDER BY delivery_id",
    )
    assert res == [("d-1", "ACCEPTED", True), ("d-2", "DUPLICATE_ISSUE", True)]


async def test_concurrent_identical_deliveries_process_once(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    results = await asyncio.gather(*[repo.record(accepted()) for _ in range(15)])
    assert results.count(Outcome.ACCEPTED) == 1 and results.count(Outcome.DUPLICATE) == 14
    assert counts(database_url) == (1, 1, 1)


async def test_concurrent_distinct_deliveries_for_same_issue_create_one_code_change_request(
    repo: SqlAlchemyWebhookRepository, database_url: str
) -> None:
    results = await asyncio.gather(*[repo.record(accepted(f"d-{i}")) for i in range(10)])
    assert results.count(Outcome.ACCEPTED) == 1 and results.count(Outcome.DUPLICATE) == 9
    assert counts(database_url) == (10, 1, 1)


async def test_failure_midway_rolls_back_everything(
    repo: SqlAlchemyWebhookRepository, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("job write failed")

    monkeypatch.setattr(repo, "_enqueue_intake", boom)
    with pytest.raises(PersistenceError):
        await repo.record(accepted())
    assert counts(database_url) == (0, 0, 0)
    monkeypatch.undo()
    assert await repo.record(accepted()) is Outcome.ACCEPTED  # retry after failure succeeds


async def test_unreachable_database_is_persistence_error_without_dsn_leak() -> None:
    eng = create_engine("postgresql+psycopg://app:hunter2@127.0.0.1:1/none")
    try:
        with pytest.raises(PersistenceError) as exc:
            await SqlAlchemyWebhookRepository(create_session_factory(eng)).record(accepted())
        assert "hunter2" not in str(exc.value)
    finally:
        await eng.dispose()


def test_database_enforces_uniqueness_and_checks(database_url: str) -> None:
    ins = (
        "INSERT INTO jira_deliveries (id, delivery_id, payload_sha256, event_type, retry_count, "
        "verification_result, processing_result) VALUES (gen_random_uuid(), %s, %s, 'e', 0, 'VERIFIED', %s)"
    )
    with psycopg.connect(libpq(database_url), autocommit=True) as conn:
        conn.execute(ins, ("raw-1", "c" * 64, "ACCEPTED"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(ins, ("raw-1", "c" * 64, "ACCEPTED"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(ins, ("raw-2", "short", "ACCEPTED"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(ins, ("raw-3", "c" * 64, "WHATEVER"))
