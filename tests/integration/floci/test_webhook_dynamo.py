"""The DynamoDB webhook intake adapter against Floci (DYNAMODB-ACCESS-PATTERNS.md 1, W1-W7).

Scenarios ported from `tests/integration/test_webhook_repository.py` (which stays until PostgreSQL is
removed). Each test runs on its own `s2116-<uuid>` table; counts come from a consistent scan."""

import asyncio
import json
from typing import Any

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
)

from ai_code_change_agent.domain.webhooks.models import (
    IgnoreReason,
    IssueRef,
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.codec import decode_timestamp
from ai_code_change_agent.persistence.dynamodb.table import deserialize_item
from ai_code_change_agent.persistence.dynamodb.webhook_repository import (
    INTAKE_JOB_KIND,
    DynamoWebhookRepository,
)

pytestmark = [pytest.mark.floci, pytest.mark.integration]
SECRET = "driver detail: arn:aws:dynamodb:us-east-1:123456789012:table/acca token=abc"


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


def items(table: Table) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for page in table._client.get_paginator("scan").paginate(
        TableName=table.name, ConsistentRead=True
    ):
        found.extend(deserialize_item(raw) or {} for raw in page["Items"])
    return found


def by_entity(table: Table) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items(table):
        grouped.setdefault(str(item["entity"]), []).append(item)
    return grouped


def counts(table: Table) -> tuple[int, int, int, int]:
    """(deliveries, requests, jobs, outbox entries)."""
    grouped = by_entity(table)
    outbox = [i for i in grouped.get("job", []) if i.get("GSI3PK") == keys.GSI_DISPATCH]
    return (
        len(grouped.get("jira_delivery", [])),
        len(grouped.get("code_change_request", [])),
        len(grouped.get("job", [])),
        len(outbox),
    )


def deliveries(table: Table) -> list[tuple[str, str, str | None]]:
    rows = by_entity(table).get("jira_delivery", [])
    return sorted((r["delivery_id"], r["processing_result"], r.get("request_id")) for r in rows)


@pytest.fixture
def repo(app_table: Table) -> DynamoWebhookRepository:
    return DynamoWebhookRepository(app_table)


async def test_accepted_writes_delivery_request_guards_and_outboxed_job_together(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    assert await repo.record(accepted()) is Outcome.ACCEPTED
    assert counts(app_table) == (1, 1, 1, 1)
    grouped = by_entity(app_table)
    (request,) = grouped["code_change_request"]
    rid = request["id"]
    assert request["status"] == "RECEIVED" and request["jira_issue_id"] == "10001"
    assert request["row_version"] == 1 and request["GSI1PK"] == "ISSUEKEY#TST-10001"
    (delivery,) = grouped["jira_delivery"]
    assert (
        delivery["request_id"],
        delivery["processing_result"],
        delivery["verification_result"],
        delivery["flow"],
        delivery["retry_count"],
        delivery["jira_issue_type"],
    ) == (rid, "ACCEPTED", "VERIFIED", "Primary", 0, "Story")
    (guard,) = grouped["jira_issue_guard"]
    assert (guard["PK"], guard["request_id"]) == ("JIRAISSUE#10001", rid)
    (job,) = grouped["job"]
    assert (job["kind"], job["status"], job["attempts"], job["max_attempts"]) == (
        INTAKE_JOB_KIND,
        "QUEUED",
        0,
        3,
    )
    assert job["dedupe_key"] == f"{INTAKE_JOB_KIND}:{rid}"
    assert json.loads(job["payload_json"]) == {
        "code_change_request_id": rid,
        "jira_issue_id": "10001",
        "jira_issue_key": "TST-10001",
    }
    assert job["GSI3SK"] == f"{job['available_at']}#{job['id']}"
    decode_timestamp(job["available_at"])
    (job_guard,) = grouped["job_dedupe_guard"]
    assert (job_guard["PK"], job_guard["job_id"]) == (f"JOBKEY#{INTAKE_JOB_KIND}:{rid}", job["id"])
    assert "expires_at" not in job and "expires_at" not in job_guard

    outbox = await app_table.query(
        key_condition="GSI3PK = :d", values={":d": keys.GSI_DISPATCH}, index="GSI3"
    )
    assert [row["PK"] for row in outbox] == [f"JOB#{job['id']}"]


async def test_ignored_writes_only_the_delivery(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    assert await repo.record(ignored()) is Outcome.IGNORED
    assert counts(app_table) == (1, 0, 0, 0)
    (row,) = items(app_table)
    assert (row["processing_result"], row["ignore_reason"]) == ("IGNORED", "event_not_supported")
    assert "jira_issue_id" not in row and "request_id" not in row and "flow" not in row


async def test_duplicate_delivery_id_changes_nothing(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    await repo.record(accepted())
    before = items(app_table)
    assert await repo.record(accepted()) is Outcome.DUPLICATE
    assert await repo.record(ignored("d-1")) is Outcome.DUPLICATE
    assert sorted(items(app_table), key=str) == sorted(before, key=str)


async def test_new_delivery_for_same_issue_is_duplicate_issue_linked_to_the_request(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    await repo.record(accepted("d-1"))
    assert await repo.record(accepted("d-2")) is Outcome.DUPLICATE
    assert counts(app_table) == (2, 1, 1, 1)
    (request,) = by_entity(app_table)["code_change_request"]
    assert deliveries(app_table) == [
        ("d-1", "ACCEPTED", request["id"]),
        ("d-2", "DUPLICATE_ISSUE", request["id"]),
    ]


async def test_twenty_concurrent_identical_deliveries_process_once(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    results = await asyncio.gather(*[repo.record(accepted()) for _ in range(20)])
    assert results.count(Outcome.ACCEPTED) == 1 and results.count(Outcome.DUPLICATE) == 19
    assert counts(app_table) == (1, 1, 1, 1)


async def test_concurrent_distinct_deliveries_for_one_issue_create_one_request(
    repo: DynamoWebhookRepository, app_table: Table
) -> None:
    results = await asyncio.gather(*[repo.record(accepted(f"d-{i:02d}")) for i in range(10)])
    assert results.count(Outcome.ACCEPTED) == 1 and results.count(Outcome.DUPLICATE) == 9
    assert counts(app_table) == (10, 1, 1, 1)
    (request,) = by_entity(app_table)["code_change_request"]
    rows = deliveries(app_table)
    assert {r[2] for r in rows} == {request["id"]}
    assert sorted(r[1] for r in rows) == ["ACCEPTED"] + ["DUPLICATE_ISSUE"] * 9


async def test_a_failed_transaction_writes_nothing_and_the_retry_is_once(
    repo: DynamoWebhookRepository, app_table: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(**_: Any) -> Any:
        raise EndpointConnectionError(endpoint_url=f"http://127.0.0.1:1/?{SECRET}")

    monkeypatch.setattr(app_table._client, "transact_write_items", unreachable)
    with pytest.raises(PersistenceError) as failure:
        await repo.record(accepted("d-1"))
    assert str(failure.value) == "EndpointConnectionError"
    assert failure.value.__cause__ is None and failure.value.__context__ is None
    monkeypatch.undo()
    assert counts(app_table) == (0, 0, 0, 0)

    assert await repo.record(accepted("d-1")) is Outcome.ACCEPTED  # Jira's retry of it
    assert await repo.record(accepted("d-2")) is Outcome.DUPLICATE  # a later new delivery
    assert counts(app_table) == (2, 1, 1, 1)


async def test_service_errors_are_persistence_errors_without_driver_text(
    repo: DynamoWebhookRepository, app_table: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    def internal(**_: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "InternalServerError", "Message": SECRET}}, "TransactWriteItems"
        )

    monkeypatch.setattr(app_table._client, "transact_write_items", internal)
    monkeypatch.setattr(app_table._client, "put_item", internal)
    for delivery in (accepted(), ignored()):
        with pytest.raises(PersistenceError) as failure:
            await repo.record(delivery)
        assert str(failure.value) == "InternalServerError"
        assert SECRET not in repr(failure.value) and failure.value.__cause__ is None
    monkeypatch.undo()
    assert counts(app_table) == (0, 0, 0, 0)


async def test_delivery_records_expire_after_the_retention_window(app_table: Table) -> None:
    """Delivery dedupe only has to outlive Jira's redelivery window: TTL `expires_at` (30 days
    by default); the Story guard and the job dedupe guard never expire."""
    repo = DynamoWebhookRepository(app_table, delivery_retention_days=2)
    assert await repo.record(accepted()) is Outcome.ACCEPTED
    assert await repo.record(ignored()) is Outcome.IGNORED
    grouped = by_entity(app_table)
    for delivery in grouped["jira_delivery"]:
        received = decode_timestamp(delivery["received_at"], "received_at")
        assert int(delivery["expires_at"]) == int(received.timestamp()) + 2 * 86_400
    for entity in ("jira_issue_guard", "job_dedupe_guard", "code_change_request"):
        assert all("expires_at" not in i for i in grouped[entity])
    with pytest.raises(ValueError, match=r"1\.\.365"):
        DynamoWebhookRepository(app_table, delivery_retention_days=0)
