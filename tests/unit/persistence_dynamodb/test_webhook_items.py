"""Pure job item builders and the webhook adapter's rare paths (botocore Stubber, no network)."""

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.domain.webhooks.models import (
    IssueRef,
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import RetryPolicy, Table
from ai_code_change_agent.persistence.dynamodb.job_items import new_job
from ai_code_change_agent.persistence.dynamodb.webhook_repository import DynamoWebhookRepository

NOW = datetime(2026, 11, 27, 9, 29, 17, 162000, tzinfo=UTC)
SECRET = "driver detail: arn:aws:dynamodb:us-east-1:123456789012:table/acca token=abc"


class FixedClock:
    def now(self) -> datetime:
        return NOW


def test_new_job_builds_guard_and_outboxed_job() -> None:
    jid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    due = NOW + timedelta(seconds=30)
    built = new_job(
        "code_change.intake",
        {"b": 1, "a": "x"},
        dedupe_key="code_change.intake:r",
        max_attempts=3,
        now=NOW,
        available_at=due,
        job_id=jid,
    )
    assert built.job_id == jid
    assert built.guard == {
        "PK": "JOBKEY#code_change.intake:r",
        "SK": "GUARD",
        "entity": "job_dedupe_guard",
        "job_id": str(jid),
        "dedupe_key": "code_change.intake:r",
        "created_at": "2026-11-27T09:29:17.162000Z",
    }
    job = built.job
    assert (job["PK"], job["SK"], job["status"], job["attempts"], job["row_version"]) == (
        f"JOB#{jid}",
        "JOB",
        "QUEUED",
        0,
        1,
    )
    assert job["GSI3PK"] == "DISPATCH"
    assert job["GSI3SK"] == f"2026-11-27T09:29:47.162000Z#{jid}"
    assert job["available_at"] == "2026-11-27T09:29:47.162000Z"
    assert job["payload_json"] == '{"b":1,"a":"x"}' and json.loads(job["payload_json"])["b"] == 1
    assert "expires_at" not in job and "lease_owner" not in job


def test_new_job_defaults_to_due_now_with_a_fresh_id() -> None:
    one = new_job("a.b", {}, dedupe_key="k", max_attempts=1, now=NOW)
    two = new_job("a.b", {}, dedupe_key="k", max_attempts=10, now=NOW)
    assert one.job_id != two.job_id
    assert one.job["available_at"] == one.job["created_at"] == "2026-11-27T09:29:17.162000Z"


@pytest.mark.parametrize(
    ("kind", "attempts", "dedupe_key"),
    [
        ("Bad", 3, "k"),
        ("ab", 3, "k"),
        ("a.b", 0, "k"),
        ("a.b", 11, "k"),
        ("a.b", True, "k"),
        ("a.b", 3, ""),
        ("a.b", 3, "k" * 256),
    ],
)
def test_new_job_refuses_what_the_jobs_table_refused(
    kind: str, attempts: int, dedupe_key: str
) -> None:
    with pytest.raises(ValueError):
        new_job(kind, {}, dedupe_key=dedupe_key, max_attempts=attempts, now=NOW)


@pytest.fixture
def stubbed() -> Iterator[tuple[DynamoWebhookRepository, Stubber]]:
    aws = AwsClients(Settings(_env_file=None, app_env="test"))
    policy = RetryPolicy(attempts=2, sleep=_no_sleep, random=lambda: 0.0)
    with Stubber(aws.dynamodb) as stub:
        table = Table(aws.dynamodb, "acca-test", retry=policy)
        yield DynamoWebhookRepository(table, clock=FixedClock()), stub
        stub.assert_no_pending_responses()


async def _no_sleep(_: float) -> None:
    return None


def accepted(delivery_id: str = "d-2") -> ReceivedDelivery:
    return ReceivedDelivery(
        delivery_id=delivery_id,
        payload_sha256="a" * 64,
        event_type="jira:issue_created",
        retry_count=0,
        flow=None,
        status=ProcessingStatus.ACCEPTED,
        ignore_reason=None,
        issue=IssueRef("10001", "TST-1", "TST", "Story"),
    )


def issue_taken(stub: Stubber, image: dict[str, Any] | None) -> None:
    reason: dict[str, Any] = {"Code": "ConditionalCheckFailed", "Message": SECRET}
    if image is not None:
        reason["Item"] = image
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message=SECRET,
        http_status_code=400,
        modeled_fields={"CancellationReasons": [{"Code": "None"}, reason] + [{"Code": "None"}] * 3},
    )


async def test_duplicate_issue_without_an_old_image_reads_the_guard(
    stubbed: tuple[DynamoWebhookRepository, Stubber],
) -> None:
    repo, stub = stubbed
    issue_taken(stub, None)
    stub.add_response(
        "get_item",
        {
            "Item": {
                "PK": {"S": "JIRAISSUE#10001"},
                "SK": {"S": "GUARD"},
                "request_id": {"S": "r-1"},
            }
        },
        {"TableName": "acca-test", "Key": ANY, "ConsistentRead": True},
    )
    sent: list[dict[str, Any]] = []
    repo._table._client.meta.events.register(
        "provide-client-params.dynamodb.PutItem", lambda params, **_: sent.append(params)
    )
    stub.add_response("put_item", {}, None)
    assert await repo.record(accepted()) is Outcome.DUPLICATE
    (params,) = sent
    assert params["Item"]["processing_result"] == {"S": "DUPLICATE_ISSUE"}
    assert params["Item"]["request_id"] == {"S": "r-1"}
    assert params["ConditionExpression"] == "attribute_not_exists(PK)"


async def test_duplicate_issue_uses_the_guard_image_without_a_read(
    stubbed: tuple[DynamoWebhookRepository, Stubber],
) -> None:
    repo, stub = stubbed
    issue_taken(stub, {"PK": {"S": "JIRAISSUE#10001"}, "request_id": {"S": "r-9"}})
    stub.add_response("put_item", {}, None)  # no get_item expected
    assert await repo.record(accepted()) is Outcome.DUPLICATE


async def test_a_missing_guard_after_a_cancellation_is_a_persistence_error(
    stubbed: tuple[DynamoWebhookRepository, Stubber],
) -> None:
    repo, stub = stubbed
    issue_taken(stub, None)
    stub.add_response("get_item", {}, None)
    with pytest.raises(PersistenceError, match=r"^issue_guard_missing$"):
        await repo.record(accepted())


async def test_lost_conflicts_become_a_persistence_error_code(
    stubbed: tuple[DynamoWebhookRepository, Stubber],
) -> None:
    repo, stub = stubbed
    for _ in range(2):
        stub.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message=SECRET,
            http_status_code=400,
            modeled_fields={"CancellationReasons": [{"Code": "TransactionConflict"}] * 5},
        )
    with pytest.raises(PersistenceError) as failure:
        await repo.record(accepted())
    assert str(failure.value) == "ConcurrentModificationError"
    assert failure.value.__cause__ is None
