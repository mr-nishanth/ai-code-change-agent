"""`DynamoSqsJobQueue` paths Floci cannot produce: botocore `Stubber`, no network (DM 10.4).

AWS rejects a stale receipt handle on `ChangeMessageVisibility` / `DeleteMessage`; Floci accepts it.
These failures must never be fatal: DynamoDB decides, the next receipt reconciles."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from botocore.stub import ANY, Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.application.jobs.queue import JobStatus
from ai_code_change_agent.domain.webhooks.models import PersistenceError
from ai_code_change_agent.persistence.dynamodb import RetryPolicy, Table, keys
from ai_code_change_agent.persistence.dynamodb.codec import encode_timestamp
from ai_code_change_agent.persistence.dynamodb.job_queue import EXHAUSTED, DynamoSqsJobQueue
from ai_code_change_agent.persistence.dynamodb.table import serialize_item

NOW = datetime(2026, 11, 27, 9, 29, 17, 162000, tzinfo=UTC)
URL = "http://127.0.0.1:1/000000000000/s2116-jobs"
SECRET = "driver detail: arn:aws:sqs:us-east-1:123456789012:acca token=abc"
JID = uuid.UUID("11111111-2222-3333-4444-555555555555")


class FixedClock:
    def now(self) -> datetime:
        return NOW


def _client(service: str) -> Any:
    session = boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    return session.client(service, endpoint_url="http://127.0.0.1:1")


@dataclass
class Stubs:
    queue: DynamoSqsJobQueue
    ddb: Stubber
    sqs: Stubber


@pytest.fixture
def stubs() -> Iterator[Stubs]:
    ddb_client, sqs_client = _client("dynamodb"), _client("sqs")
    table = Table(ddb_client, "s2116-app", retry=RetryPolicy(attempts=1))
    queue = DynamoSqsJobQueue(table, sqs_client, URL, clock=FixedClock(), receive_wait_seconds=0)
    with Stubber(ddb_client) as ddb, Stubber(sqs_client) as sqs:
        yield Stubs(queue, ddb, sqs)
        ddb.assert_no_pending_responses()
        sqs.assert_no_pending_responses()


def job_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        **keys.job(JID).as_dict(),
        "entity": "job",
        "id": str(JID),
        "kind": "k.test",
        "payload_json": '{"x":1}',
        "status": "RUNNING",
        "attempts": 1,
        "max_attempts": 3,
        "available_at": encode_timestamp(NOW - timedelta(minutes=5)),
        "lease_owner": "w1",
        "lease_expires_at": encode_timestamp(NOW + timedelta(seconds=60)),
        "row_version": 3,
    }
    item.update(overrides)
    return {k: v for k, v in item.items() if v is not None}


def message(body: str | None = None) -> dict[str, Any]:
    return {
        "Messages": [
            {
                "MessageId": "m-1",
                "ReceiptHandle": "receipt-1",
                "Body": body if body is not None else json.dumps({"job_id": str(JID)}),
            }
        ]
    }


def condition_failed(stub: Stubber, operation: str, old: dict[str, Any] | None) -> None:
    fields = {"Item": serialize_item(old)} if old is not None else {}
    stub.add_client_error(
        operation, "ConditionalCheckFailedException", SECRET, 400, modeled_fields=fields
    )


def receive(s: Stubs, body: str | None = None, lease: int = 60) -> None:
    s.sqs.add_response(
        "receive_message",
        message(body),
        {
            "QueueUrl": URL,
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 0,
            "VisibilityTimeout": lease,
        },
    )


def claimed(s: Stubs) -> None:
    receive(s)
    s.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})


def visibility(s: Stubs, seconds: int, *, error: str | None = None) -> None:
    params = {"QueueUrl": URL, "ReceiptHandle": "receipt-1", "VisibilityTimeout": seconds}
    if error:
        s.sqs.add_client_error(
            "change_message_visibility", error, SECRET, 400, expected_params=params
        )
    else:
        s.sqs.add_response("change_message_visibility", {}, params)


async def test_an_invalid_receipt_on_heartbeat_visibility_is_not_fatal(stubs: Stubs) -> None:
    claimed(stubs)
    job = await stubs.queue.claim("w1", 60)
    assert job is not None and job.id == JID and job.payload == {"x": 1} and job.attempts == 1
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    visibility(stubs, 60, error="ReceiptHandleIsInvalid")
    assert await stubs.queue.heartbeat(JID, "w1", 60)  # DynamoDB said yes; SQS is best effort


async def test_a_lost_heartbeat_returns_false_and_forgets_the_receipt(stubs: Stubs) -> None:
    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    condition_failed(stubs.ddb, "update_item", None)
    assert not await stubs.queue.heartbeat(JID, "w1", 60)
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    assert await stubs.queue.complete(JID, "w1")  # no receipt left: no DeleteMessage call


async def test_retry_visibility_and_delete_failures_are_not_fatal(stubs: Stubs) -> None:
    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    visibility(stubs, 900, error="MessageNotInflight")
    assert await stubs.queue.fail(JID, "w1", "x", retry_in_seconds=900) is JobStatus.QUEUED

    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    stubs.sqs.add_client_error("delete_message", "ReceiptHandleIsInvalid", SECRET, 400)
    assert await stubs.queue.complete(JID, "w1")


async def test_a_retry_beyond_the_sqs_maximum_is_capped(stubs: Stubs) -> None:
    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    visibility(stubs, 43_200)
    assert await stubs.queue.fail(JID, "w1", "x", retry_in_seconds=50_000) is JobStatus.QUEUED


async def test_fail_without_attempts_left_is_dead_and_a_lost_lease_is_running(
    stubs: Stubs,
) -> None:
    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    condition_failed(stubs.ddb, "update_item", job_item(attempts=3))  # attempts exhausted
    stubs.ddb.add_response("update_item", {"Attributes": serialize_item(job_item())})
    stubs.sqs.add_response("delete_message", {}, {"QueueUrl": URL, "ReceiptHandle": "receipt-1"})
    assert await stubs.queue.fail(JID, "w1", "x", retry_in_seconds=30) is JobStatus.DEAD

    condition_failed(stubs.ddb, "update_item", job_item(lease_owner="w2"))
    assert await stubs.queue.fail(JID, "w1", "x", retry_in_seconds=30) is JobStatus.RUNNING
    condition_failed(stubs.ddb, "update_item", None)
    assert await stubs.queue.fail(JID, "w1", "x", retry_in_seconds=None) is JobStatus.RUNNING


async def test_an_enqueue_whose_send_fails_stays_in_the_outbox(stubs: Stubs) -> None:
    stubs.ddb.add_response("transact_write_items", {})
    stubs.sqs.add_client_error("send_message", "AWS.SimpleQueueService.NonExistentQueue", SECRET)
    # no update_item queued: the outbox keys stay for the relay
    assert await stubs.queue.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)


async def test_an_sqs_outage_on_receive_is_a_code_without_driver_text(stubs: Stubs) -> None:
    stubs.sqs.add_client_error("receive_message", "ServiceUnavailable", SECRET, 503)
    with pytest.raises(PersistenceError) as raised:
        await stubs.queue.claim("w1", 60)
    assert str(raised.value) == "ServiceUnavailable" and raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"job_id": "nope"}', '{"job_id": "x", "extra": 1}', ""]
)
async def test_a_message_that_names_no_job_is_left_for_the_dlq(stubs: Stubs, body: str) -> None:
    receive(stubs, body)
    assert await stubs.queue.claim("w1", 60) is None  # no DynamoDB call, no SQS call


async def test_an_empty_receive_is_none(stubs: Stubs) -> None:
    stubs.sqs.add_response("receive_message", {})
    assert await stubs.queue.claim("w1", 60) is None


async def test_a_message_for_an_unknown_job_is_left_for_the_dlq(stubs: Stubs) -> None:
    receive(stubs)
    condition_failed(stubs.ddb, "update_item", None)
    stubs.ddb.add_response("get_item", {})
    assert await stubs.queue.claim("w1", 60) is None


@pytest.mark.parametrize("status", ["SUCCEEDED", "DEAD"])
async def test_a_message_for_a_terminal_job_is_deleted(stubs: Stubs, status: str) -> None:
    receive(stubs)
    condition_failed(stubs.ddb, "update_item", job_item(status=status, lease_owner=None))
    stubs.sqs.add_response("delete_message", {}, {"QueueUrl": URL, "ReceiptHandle": "receipt-1"})
    assert await stubs.queue.claim("w1", 60) is None


async def test_a_not_due_job_hides_its_message_until_due(stubs: Stubs) -> None:
    receive(stubs)
    due = encode_timestamp(NOW + timedelta(seconds=120.2))
    condition_failed(stubs.ddb, "update_item", job_item(status="QUEUED", available_at=due))
    visibility(stubs, 121)
    assert await stubs.queue.claim("w1", 60) is None


async def test_a_job_leased_elsewhere_hides_its_message_for_the_remaining_lease(
    stubs: Stubs,
) -> None:
    receive(stubs)
    lease = encode_timestamp(NOW + timedelta(seconds=42))
    condition_failed(stubs.ddb, "update_item", job_item(lease_owner="w2", lease_expires_at=lease))
    visibility(stubs, 42)
    assert await stubs.queue.claim("w1", 60) is None


async def test_an_exhausted_expired_job_becomes_dead_and_its_message_is_deleted(
    stubs: Stubs,
) -> None:
    receive(stubs)
    expired = encode_timestamp(NOW - timedelta(seconds=1))
    condition_failed(stubs.ddb, "update_item", job_item(attempts=3, lease_expires_at=expired))
    stubs.ddb.add_response(
        "update_item",
        {"Attributes": {}},
        {
            "TableName": "s2116-app",
            "Key": serialize_item(keys.job(JID).as_dict()),
            "UpdateExpression": ANY,
            "ConditionExpression": "row_version = :seen",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": ANY,
            "ReturnValues": "ALL_NEW",
        },
    )
    stubs.sqs.add_response("delete_message", {}, {"QueueUrl": URL, "ReceiptHandle": "receipt-1"})
    assert await stubs.queue.claim("w1", 60) is None
    assert EXHAUSTED == "lease_expired_attempts_exhausted"


async def test_an_exhausted_job_that_changed_meanwhile_is_left_alone(stubs: Stubs) -> None:
    receive(stubs)
    expired = encode_timestamp(NOW - timedelta(seconds=1))
    condition_failed(stubs.ddb, "update_item", job_item(attempts=3, lease_expires_at=expired))
    condition_failed(stubs.ddb, "update_item", None)  # row_version moved: no DeleteMessage
    assert await stubs.queue.claim("w1", 60) is None


async def test_a_queued_job_without_attempts_left_is_dead(stubs: Stubs) -> None:
    receive(stubs)
    condition_failed(stubs.ddb, "update_item", job_item(status="QUEUED", attempts=3))
    stubs.ddb.add_response("update_item", {"Attributes": {}})
    stubs.sqs.add_response("delete_message", {}, {"QueueUrl": URL, "ReceiptHandle": "receipt-1"})
    assert await stubs.queue.claim("w1", 60) is None


async def test_a_job_that_became_claimable_is_delivered_again_at_once(stubs: Stubs) -> None:
    receive(stubs)
    expired = encode_timestamp(NOW - timedelta(seconds=1))
    condition_failed(stubs.ddb, "update_item", None)  # no old image returned
    stubs.ddb.add_response("get_item", {"Item": serialize_item(job_item(lease_expires_at=expired))})
    visibility(stubs, 0)
    assert await stubs.queue.claim("w1", 60) is None


async def test_our_own_job_received_again_keeps_the_newest_receipt(stubs: Stubs) -> None:
    claimed(stubs)
    await stubs.queue.claim("w1", 60)
    stubs.sqs.add_response(
        "receive_message",
        {
            "Messages": [
                {
                    "MessageId": "m-1",
                    "ReceiptHandle": "receipt-2",
                    "Body": message()["Messages"][0]["Body"],
                }
            ]
        },
    )
    condition_failed(stubs.ddb, "update_item", job_item())  # leased by w1, live
    stubs.sqs.add_response(
        "change_message_visibility",
        {},
        {"QueueUrl": URL, "ReceiptHandle": "receipt-2", "VisibilityTimeout": 60},
    )
    assert await stubs.queue.claim("w1", 60) is None
    stubs.ddb.add_response("update_item", {"Attributes": {}})
    stubs.sqs.add_response("delete_message", {}, {"QueueUrl": URL, "ReceiptHandle": "receipt-2"})
    assert await stubs.queue.complete(JID, "w1")


async def test_relay_sends_due_outbox_jobs_and_skips_stale_index_rows(stubs: Stubs) -> None:
    other = uuid.uuid4()
    rows = [
        serialize_item({**keys.job(JID).as_dict(), "GSI3PK": "DISPATCH", "GSI3SK": "x"}),
        serialize_item({**keys.job(other).as_dict(), "GSI3PK": "DISPATCH", "GSI3SK": "y"}),
        serialize_item({"PK": "JOB#not-a-uuid", "SK": "JOB", "GSI3PK": "DISPATCH", "GSI3SK": "z"}),
    ]
    stubs.ddb.add_response("query", {"Items": rows})
    stubs.ddb.add_response(
        "get_item", {"Item": serialize_item(job_item(status="QUEUED", GSI3PK="DISPATCH"))}
    )
    stubs.sqs.add_response(
        "send_message",
        {"MessageId": "m"},
        {"QueueUrl": URL, "MessageBody": json.dumps({"job_id": str(JID)})},
    )
    stubs.ddb.add_response("update_item", {"Attributes": {}})
    stubs.ddb.add_response("get_item", {"Item": serialize_item(job_item(GSI3PK="SWEEP"))})
    assert await stubs.queue.dispatch_pending() == 1


async def test_relay_counts_only_messages_that_were_sent(stubs: Stubs) -> None:
    rows = [serialize_item({**keys.job(JID).as_dict(), "GSI3PK": "DISPATCH", "GSI3SK": "x"})]
    stubs.ddb.add_response("query", {"Items": rows})
    stubs.ddb.add_response(
        "get_item", {"Item": serialize_item(job_item(status="QUEUED", GSI3PK="DISPATCH"))}
    )
    stubs.sqs.add_client_error("send_message", "ServiceUnavailable", SECRET, 503)
    assert await stubs.queue.dispatch_pending() == 0


@pytest.mark.parametrize(
    ("overrides", "resent"),
    [({"lease_expires_at": encode_timestamp(NOW - timedelta(seconds=301))}, True),
     ({"lease_expires_at": encode_timestamp(NOW - timedelta(seconds=299))}, False),
     ({"status": "QUEUED", "available_at": encode_timestamp(NOW - timedelta(hours=1))}, True),
     ({"status": "QUEUED", "available_at": encode_timestamp(NOW - timedelta(hours=1)),
       "dispatched_at": encode_timestamp(NOW - timedelta(seconds=10))}, False),
     ({"status": "QUEUED", "available_at": encode_timestamp(NOW - timedelta(hours=1)),
       "dispatched_at": encode_timestamp(NOW - timedelta(hours=1))}, True),
     ({"status": "SUCCEEDED"}, False),
     ({"GSI3PK": "DISPATCH"}, False)],
)  # fmt: skip
async def test_sweep_resends_only_overdue_live_jobs(
    stubs: Stubs, overrides: dict[str, Any], resent: bool
) -> None:
    rows = [serialize_item({**keys.job(JID).as_dict(), "GSI3PK": "SWEEP", "GSI3SK": "x"})]
    stubs.ddb.add_response("query", {"Items": rows})
    stubs.ddb.add_response(
        "get_item", {"Item": serialize_item(job_item(**{"GSI3PK": "SWEEP", **overrides}))}
    )
    if resent:
        stubs.sqs.add_response("send_message", {"MessageId": "m"})
        stubs.ddb.add_response("update_item", {"Attributes": {}})
    assert await stubs.queue.sweep() == int(resent)


async def test_sweep_ignores_a_row_whose_job_is_gone(stubs: Stubs) -> None:
    rows = [serialize_item({**keys.job(JID).as_dict(), "GSI3PK": "SWEEP", "GSI3SK": "x"})]
    stubs.ddb.add_response("query", {"Items": rows})
    stubs.ddb.add_response("get_item", {})
    assert await stubs.queue.sweep() == 0


async def test_the_queue_url_is_resolved_once_and_its_failure_is_a_code() -> None:
    ddb_client, sqs_client = _client("dynamodb"), _client("sqs")
    calls: list[int] = []

    def resolve() -> str:
        calls.append(1)
        return URL

    queue = DynamoSqsJobQueue(Table(ddb_client, "t"), sqs_client, resolve, receive_wait_seconds=0)
    with Stubber(sqs_client) as sqs:
        sqs.add_response(
            "receive_message",
            {},
            {
                "QueueUrl": URL,
                "MaxNumberOfMessages": 1,
                "WaitTimeSeconds": 0,
                "VisibilityTimeout": 60,
            },
        )
        sqs.add_response(
            "receive_message",
            {},
            {
                "QueueUrl": URL,
                "MaxNumberOfMessages": 1,
                "WaitTimeSeconds": 0,
                "VisibilityTimeout": 60,
            },
        )
        assert await queue.claim("w", 60) is None and await queue.claim("w", 60) is None
    assert calls == [1]

    def broken() -> str:
        return str(sqs_client.get_queue_url(QueueName="missing"))

    failing = DynamoSqsJobQueue(Table(ddb_client, "t"), sqs_client, broken)
    with Stubber(sqs_client) as sqs:
        sqs.add_client_error("get_queue_url", "AWS.SimpleQueueService.NonExistentQueue", SECRET)
        with pytest.raises(PersistenceError) as raised:
            await failing.claim("w", 60)
    assert str(raised.value) == "AWS.SimpleQueueService.NonExistentQueue"


def test_settings_out_of_range_are_refused() -> None:
    table = Table(_client("dynamodb"), "t")
    with pytest.raises(ValueError, match="receive_wait_seconds"):
        DynamoSqsJobQueue(table, object(), URL, receive_wait_seconds=21)
    with pytest.raises(ValueError, match="sweep_grace_seconds"):
        DynamoSqsJobQueue(table, object(), URL, sweep_grace_seconds=-1)
