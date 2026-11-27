"""PostgreSQL: Jira being unavailable, or refusing, while the worker reads the Story at intake.

The intake job runs through the real worker, job queue, change store and registry; only Jira is a
fake. A transient Jira failure must leave the request untouched (no repository assignment, no
attempt), be retried by the queue with a fixed error code, and a later retry must start exactly
one attempt.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.jira.errors import (
    JiraAuthenticationError,
    JiraIntegrationError,
    JiraPermissionError,
    JiraRateLimitError,
    JiraResponseError,
    JiraServiceUnavailableError,
    JiraTimeoutError,
)
from ai_code_change_agent.application.jobs.worker import Worker
from ai_code_change_agent.domain.webhooks.models import (
    IssueRef,
    Outcome,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.orchestration.intake import INTAKE, IntakeHandler
from ai_code_change_agent.persistence.repositories.change_store import SqlChangeUnitOfWork
from ai_code_change_agent.persistence.repositories.job_queue import PostgresJobQueue
from ai_code_change_agent.persistence.repositories.webhook_repository import (
    SqlAlchemyWebhookRepository,
)
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import query
from tests.support.changes import python_profile
from tests.support.registry_fixtures import new_repo
from tests.support.sql_registry import sql_registry_service, sql_repository_resolver
from tests.support.workflow import FakeJira

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


class FlakyJira(FakeJira):
    """Jira that fails the first `failures` Story reads, then answers normally."""

    def __init__(self, error: JiraIntegrationError, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.reads = 0

    async def get_issue(self, issue_ref: str) -> Any:
        self.reads += 1
        if self.reads <= self.failures:
            raise self.error
        return await super().get_issue(issue_ref)


async def accepted_story(sessions: async_sessionmaker[AsyncSession]) -> None:
    reg = sql_registry_service(sessions)
    await reg.register_repository(new_repo("shop-service", pid="424242"), actor="cli:t")
    await reg.create_version("shop-service", python_profile(), actor="cli:t")
    await reg.publish_version("shop-service", 1, actor="cli:t")
    await reg.map_project("SHOP", "shop-service", actor="cli:t")
    await reg.enable("shop-service", actor="cli:t")
    delivery = ReceivedDelivery(
        delivery_id="d-intake",
        payload_sha256="a" * 64,
        event_type="jira:issue_created",
        retry_count=0,
        flow="Primary",
        status=ProcessingStatus.ACCEPTED,
        ignore_reason=None,
        issue=IssueRef(
            issue_id="10001", issue_key="SHOP-1", project_key="SHOP", issue_type="Story"
        ),
    )
    assert await SqlAlchemyWebhookRepository(sessions).record(delivery) is Outcome.ACCEPTED


def intake_worker(
    sessions: async_sessionmaker[AsyncSession], jira: FakeJira, started: list[uuid.UUID]
) -> Worker:
    async def start(attempt_id: uuid.UUID) -> None:
        started.append(attempt_id)

    handler = IntakeHandler(
        changes=CodeChangeService(lambda: SqlChangeUnitOfWork(sessions), max_repairs=1),
        jira=jira,
        resolver=sql_repository_resolver(sessions),
        start=start,
        repository_field_configured=False,
    )
    return Worker(
        PostgresJobQueue(sessions), {INTAKE: handler}, lease_seconds=30, heartbeat_seconds=10
    )


def state(url: str) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]], int]:
    jobs = query(url, "SELECT status, attempts, last_error FROM jobs")
    requests = query(url, "SELECT status, repository_id IS NULL FROM code_change_requests")
    attempts = query(url, "SELECT count(*) FROM code_change_attempts")[0][0]
    return jobs, requests, int(attempts)  # type: ignore[call-overload]


def make_due(url: str) -> None:
    query(url, "UPDATE jobs SET available_at = now() RETURNING id")


ERRORS = [
    JiraTimeoutError("request_timeout", operation="get_issue"),
    JiraServiceUnavailableError("connection_error", operation="get_issue"),
    JiraServiceUnavailableError("server_error", operation="get_issue", status_code=503),
    JiraRateLimitError(operation="get_issue", status_code=429),
    JiraResponseError("unexpected_status", operation="get_issue", status_code=408),
    JiraResponseError("unexpected_content_type", operation="get_issue"),
    JiraResponseError("malformed_json", operation="get_issue"),
]


@pytest.mark.parametrize(
    "error",
    ERRORS,
    ids=["timeout", "connection", "http_503", "http_429", "http_408", "html_page", "malformed"],
)
async def test_jira_unavailable_during_intake_is_retried_and_then_starts_exactly_one_attempt(
    sessions: async_sessionmaker[AsyncSession], database_url: str, error: JiraIntegrationError
) -> None:
    await accepted_story(sessions)
    jira, started = FlakyJira(error, failures=1), list[uuid.UUID]()
    worker = intake_worker(sessions, jira, started)

    assert await worker.run_once() is not None
    # fixed code or class name only: never Jira text or connection details
    code = error.code if isinstance(error, JiraResponseError) else type(error).__name__
    assert state(database_url) == ([("QUEUED", 1, code)], [("RECEIVED", True)], 0)
    assert started == []  # no half-created assignment or attempt
    assert await worker.run_once() is None  # backed off, not retried at once

    make_due(database_url)
    assert await worker.run_once() is not None
    jobs, requests, attempts = state(database_url)
    assert jobs == [("SUCCEEDED", 2, None)] and attempts == 1
    assert requests[0][1] is False  # the repository is assigned on the successful retry only
    (attempt_id,) = query(database_url, "SELECT id FROM code_change_attempts")[0]
    assert started == [attempt_id] and jira.reads == 2
    assert await worker.run_once() is None  # nothing left to run


async def test_jira_unavailable_for_every_attempt_ends_dead_with_the_request_untouched(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await accepted_story(sessions)
    jira, started = FlakyJira(JiraTimeoutError("request_timeout"), failures=99), list[uuid.UUID]()
    worker = intake_worker(sessions, jira, started)
    for _ in range(3):  # the webhook repository enqueues intake with max_attempts=3
        make_due(database_url)
        assert await worker.run_once() is not None
    assert state(database_url) == ([("DEAD", 3, "JiraTimeoutError")], [("RECEIVED", True)], 0)
    make_due(database_url)
    assert await worker.run_once() is None and jira.reads == 3 and started == []


PERMANENT = [
    (JiraAuthenticationError(operation="get_issue", status_code=401), "jira_authentication_failed"),
    (JiraPermissionError(operation="get_issue", status_code=403), "jira_permission_denied"),
    (JiraResponseError("unexpected_json_shape", operation="get_issue"), "jira_response_invalid"),
]


@pytest.mark.parametrize(("error", "code"), PERMANENT, ids=["http_401", "http_403", "bad_shape"])
async def test_a_permanent_jira_error_during_intake_is_dead_after_one_attempt(
    sessions: async_sessionmaker[AsyncSession],
    database_url: str,
    error: JiraIntegrationError,
    code: str,
) -> None:
    await accepted_story(sessions)
    jira, started = FlakyJira(error, failures=99), list[uuid.UUID]()
    worker = intake_worker(sessions, jira, started)

    assert await worker.run_once() is not None
    assert state(database_url) == ([("DEAD", 1, code)], [("RECEIVED", True)], 0)
    make_due(database_url)
    assert await worker.run_once() is None  # never claimed again: no further Jira reads
    assert jira.reads == 1 and started == []
