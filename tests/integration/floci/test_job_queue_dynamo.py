"""The DynamoDB + SQS job queue against Floci (DYNAMODB-DATA-MODEL.md 8, J1-J8).

The contract of `tests/integration/test_jobs_and_changes_db.py` (which stays until PostgreSQL is
removed), plus the outbox relay, the sweep, duplicate/late messages and the DLQ. Leases and retries
are real (a few seconds) instead of SQL clock edits: the message's visibility follows the lease.
Each test has its own `s2116-<uuid>` table, queue and DLQ."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from ai_code_change_agent.application.jobs.queue import Job, JobStatus
from ai_code_change_agent.application.jobs.worker import Worker
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.clock import SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import decode_timestamp
from ai_code_change_agent.persistence.dynamodb.job_items import new_job
from ai_code_change_agent.persistence.dynamodb.job_queue import EXHAUSTED, DynamoSqsJobQueue
from tests.support.dynamo import JobQueues, create_job_queues, delete_job_queues

pytestmark = [pytest.mark.floci, pytest.mark.integration]


def make(table: Table, queues: JobQueues, sqs: Any = None, **kwargs: Any) -> DynamoSqsJobQueue:
    kwargs.setdefault("receive_wait_seconds", 1)
    return DynamoSqsJobQueue(table, sqs or queues.sqs, queues.url, **kwargs)


async def item(table: Table, job_id: uuid.UUID) -> dict[str, Any]:
    found = await table.get(keys.job(job_id))
    assert found is not None
    return found


async def job_id_of(table: Table, dedupe_key: str) -> uuid.UUID:
    guard = await table.get(keys.job_dedupe_guard(dedupe_key))
    assert guard is not None
    return uuid.UUID(guard["job_id"])


def messages(queues: JobQueues, url: str | None = None) -> int:
    """Visible plus in-flight messages (Floci reports both exactly)."""
    attributes = queues.sqs.get_queue_attributes(
        QueueUrl=url or queues.url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]) + int(
        attributes["ApproximateNumberOfMessagesNotVisible"]
    )


def send(queues: JobQueues, body: str) -> None:
    queues.sqs.send_message(QueueUrl=queues.url, MessageBody=body)


async def test_enqueue_is_deduplicated_permanently(app_table: Table, job_queues: JobQueues) -> None:
    q = make(app_table, job_queues)
    assert await q.enqueue("code_change.intake", {"a": 1}, dedupe_key="k1", max_attempts=3)
    assert not await q.enqueue("code_change.intake", {"a": 2}, dedupe_key="k1", max_attempts=3)
    job = await q.claim("w", 60)
    assert job is not None and job.payload == {"a": 1} and await q.complete(job.id, "w")
    # finished jobs keep their key: a redelivered webhook or a re-advancing coordinator is a no-op
    assert not await q.enqueue("code_change.intake", {"a": 3}, dedupe_key="k1", max_attempts=3)
    guard = await app_table.get(keys.job_dedupe_guard("k1"))
    assert guard is not None and "expires_at" not in guard and messages(job_queues) == 0


async def test_enqueue_sends_at_once_and_leaves_the_outbox(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    assert await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    stored = await item(app_table, await job_id_of(app_table, "d"))
    assert stored["GSI3PK"] == keys.GSI_SWEEP and stored["status"] == "QUEUED"
    assert "dispatched_at" in stored and stored["row_version"] == 2
    assert messages(job_queues) == 1
    assert await q.dispatch_pending() == 0  # nothing left in the outbox


async def test_concurrent_workers_never_claim_the_same_job(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    for i in range(5):
        await q.enqueue("k.test", {"i": i}, dedupe_key=f"d{i}", max_attempts=3)
    claims = await asyncio.gather(*[q.claim(f"w{i}", 60) for i in range(12)])
    got = [c for c in claims if c is not None]
    assert len(got) == 5 and len({c.id for c in got}) == 5 and all(c.attempts == 1 for c in got)


async def test_lease_heartbeat_completion_and_owner_checks(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    job = await q.claim("w1", 60)
    assert job is not None
    assert await q.heartbeat(job.id, "w1", 60) and not await q.heartbeat(job.id, "w2", 60)
    assert not await q.complete(job.id, "w2")
    assert await q.complete(job.id, "w1")
    stored = await item(app_table, job.id)
    assert stored["status"] == "SUCCEEDED" and "lease_owner" not in stored
    assert "GSI3PK" not in stored and "GSI3SK" not in stored and int(stored["expires_at"]) > 0
    assert messages(job_queues) == 0  # the message was deleted
    assert await q.claim("w1", 60) is None


async def test_duplicate_and_late_messages_are_no_ops(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    jid = await job_id_of(app_table, "d")
    send(job_queues, json.dumps({"job_id": str(jid)}))  # a relay or sweep re-send
    first = await q.claim("w1", 3)
    assert first is not None and first.id == jid
    assert await q.claim("w2", 3) is None  # the duplicate: leased elsewhere, hidden for the lease
    assert await q.complete(jid, "w1")
    await asyncio.sleep(3.5)  # the duplicate becomes visible again: a late redelivery
    assert await q.claim("w3", 3) is None  # terminal: deleted, nothing changes
    stored = await item(app_table, jid)
    assert (stored["status"], stored["attempts"]) == ("SUCCEEDED", 1)
    assert messages(job_queues) == 0


async def test_a_crashed_workers_job_is_reclaimed_after_the_lease_expires(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    first = await q.claim("crashed", 2)
    assert first is not None and await q.claim("w2", 2) is None  # lease still valid
    await asyncio.sleep(2.5)
    again = await q.claim("w2", 60)
    assert again is not None and again.id == first.id and again.attempts == 2
    assert not await q.heartbeat(first.id, "crashed", 60)  # the old owner lost the lease


async def test_retry_backoff_then_dead_when_attempts_are_exhausted(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=2)
    job = await q.claim("w", 60)
    assert job is not None
    assert await q.fail(job.id, "w", "temporary", retry_in_seconds=2) is JobStatus.QUEUED
    stored = await item(app_table, job.id)
    assert stored["status"] == "QUEUED" and "lease_owner" not in stored
    assert await q.claim("w", 60) is None  # not due yet: the same message is hidden
    await asyncio.sleep(2.5)
    job = await q.claim("w", 60)
    assert job is not None and job.attempts == 2
    assert await q.fail(job.id, "w", "temporary", retry_in_seconds=1) is JobStatus.DEAD
    stored = await item(app_table, job.id)
    assert (stored["status"], stored["last_error"]) == ("DEAD", "temporary")
    assert "GSI3PK" not in stored and messages(job_queues) == 0


async def test_a_not_due_job_hides_a_duplicate_message_until_it_is_due(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    job = await q.claim("w", 60)
    assert job is not None
    assert await q.fail(job.id, "w", "temporary", retry_in_seconds=3600) is JobStatus.QUEUED
    send(job_queues, json.dumps({"job_id": str(job.id)}))
    assert await q.claim("w", 60) is None  # received, not due: hidden until available_at
    assert await q.claim("w", 60) is None  # both messages are now in flight
    assert messages(job_queues) == 2
    stored = await item(app_table, job.id)
    delay = decode_timestamp(stored["available_at"]) - decode_timestamp(stored["updated_at"])
    assert timedelta(seconds=3599) < delay <= timedelta(seconds=3600)


class _RetryAfter(Exception):  # noqa: N818 - carries only the attribute the worker reads
    def __init__(self, seconds: float) -> None:
        super().__init__("raw text never stored")
        self.retry_after_seconds = seconds


@pytest.mark.parametrize(("hint", "expected"), [(120, 120), (5000, 900)])
async def test_a_retry_after_hint_is_honoured_up_to_900_seconds(
    app_table: Table, job_queues: JobQueues, hint: int, expected: int
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)

    async def limited(job: Job) -> None:
        raise _RetryAfter(hint)

    w = Worker(q, {"k.test": limited}, lease_seconds=30, heartbeat_seconds=10)
    assert await w.run_once() is not None
    stored = await item(app_table, await job_id_of(app_table, "d"))
    delay = decode_timestamp(stored["available_at"]) - decode_timestamp(stored["updated_at"])
    assert stored["status"] == "QUEUED" and stored["last_error"] == "_RetryAfter"
    assert delay == timedelta(seconds=expected)
    assert await q.claim("w", 30) is None and messages(job_queues) == 1  # hidden, not lost


async def test_an_expired_job_without_attempts_left_is_dead(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=1)
    assert await q.claim("crashed", 1) is not None
    await asyncio.sleep(1.5)
    assert await q.claim("w2", 60) is None
    stored = await item(app_table, await job_id_of(app_table, "d"))
    assert (stored["status"], stored["last_error"]) == ("DEAD", EXHAUSTED)
    assert "lease_owner" not in stored and "GSI3PK" not in stored and messages(job_queues) == 0


async def test_an_expired_lease_is_reclaimed_by_one_worker_and_a_second_expiry_is_dead(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=2)
    first = await q.claim("crashed-1", 1)
    assert first is not None and first.attempts == 1
    await asyncio.sleep(1.5)
    send(job_queues, json.dumps({"job_id": str(first.id)}))  # a sweep re-send competes too
    claims = await asyncio.gather(*[q.claim(f"w{i}", 4) for i in range(8)])
    (winner,) = [c for c in claims if c is not None]  # exactly one reclaims it
    assert winner.id == first.id and winner.attempts == 2
    owner = f"w{claims.index(winner)}"
    stored = await item(app_table, first.id)
    assert (stored["status"], stored["attempts"], stored["lease_owner"]) == ("RUNNING", 2, owner)

    await asyncio.sleep(4.5)  # the second worker crashes too: its last attempt is used
    for _ in range(3):
        await q.claim("w-late", 4)
    stored = await item(app_table, first.id)
    assert (stored["status"], stored["attempts"], stored["last_error"]) == ("DEAD", 2, EXHAUSTED)
    assert not await q.complete(winner.id, owner)  # a late finish cannot revive it
    assert await q.claim("w-later", 1) is None


async def test_a_permanent_failure_is_dead_and_fail_requires_the_owner(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    job = await q.claim("w", 60)
    assert job is not None
    assert await q.fail(job.id, "other", "x", retry_in_seconds=None) is JobStatus.RUNNING
    assert await q.fail(job.id, "other", "x", retry_in_seconds=5) is JobStatus.RUNNING
    assert await q.fail(job.id, "w", "bad_input", retry_in_seconds=None) is JobStatus.DEAD
    assert messages(job_queues) == 0


async def _outbox_only(table: Table, dedupe_key: str) -> uuid.UUID:
    """What the API's intake transaction (or a producer that crashed before sending) leaves."""
    job = new_job(
        "k.test", {"x": 1}, dedupe_key=dedupe_key, max_attempts=3, now=SystemClock().now()
    )
    tx = table.transaction()
    tx.put(job.guard, condition="attribute_not_exists(PK)")
    tx.put(job.job, condition="attribute_not_exists(PK)")
    await tx.commit()
    return job.job_id


async def test_a_job_whose_send_was_skipped_is_dispatched_by_the_relay(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    jid = await _outbox_only(app_table, "crashed-before-send")
    assert await q.claim("w", 60) is None and messages(job_queues) == 0
    assert await q.dispatch_pending() == 1
    stored = await item(app_table, jid)
    assert stored["GSI3PK"] == keys.GSI_SWEEP and "dispatched_at" in stored
    assert await q.dispatch_pending() == 0  # sent once
    job = await q.claim("w", 60)
    assert job is not None and job.id == jid and job.payload == {"x": 1}


class _NoSend:
    """The SQS client of a producer whose SendMessage fails after the commit."""

    def __init__(self, sqs: Any) -> None:
        self._sqs = sqs

    def send_message(self, **_: Any) -> Any:
        raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"}}, "SendMessage")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sqs, name)


async def test_a_failed_send_after_commit_leaves_the_job_for_the_relay(
    app_table: Table, job_queues: JobQueues
) -> None:
    producer = make(app_table, job_queues, sqs=_NoSend(job_queues.sqs))
    assert await producer.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    jid = await job_id_of(app_table, "d")
    assert (await item(app_table, jid))["GSI3PK"] == keys.GSI_DISPATCH
    worker_side = make(app_table, job_queues)
    assert await worker_side.dispatch_pending() == 1
    job = await worker_side.claim("w", 60)
    assert job is not None and job.id == jid


class ShiftedClock:
    """The system clock moved forward by `offset`: overdue without waiting for the grace period."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return SystemClock().now() + self.offset


async def test_a_deleted_message_is_resent_by_the_sweep(
    app_table: Table, job_queues: JobQueues
) -> None:
    clock = ShiftedClock()
    q = make(app_table, job_queues, clock=clock, sweep_grace_seconds=300)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    jid = await job_id_of(app_table, "d")
    received = job_queues.sqs.receive_message(QueueUrl=job_queues.url, WaitTimeSeconds=1)
    (lost,) = received["Messages"]
    job_queues.sqs.delete_message(QueueUrl=job_queues.url, ReceiptHandle=lost["ReceiptHandle"])
    assert await q.claim("w", 60) is None and messages(job_queues) == 0
    assert await q.sweep() == 0  # within the grace period
    clock.offset = timedelta(seconds=301)
    assert await q.sweep() == 1
    assert await q.sweep() == 0  # just re-sent: not again within the grace period
    clock.offset = timedelta(0)
    job = await q.claim("w", 60)
    assert job is not None and job.id == jid and job.attempts == 1
    clock.offset = timedelta(seconds=200)
    assert await q.sweep() == 0  # RUNNING with a lease that has not expired by the grace...
    assert await q.complete(jid, "w")
    clock.offset = timedelta(days=1)
    assert await q.sweep() == 0  # ...and a terminal job is out of the index
    assert messages(job_queues) == 0


async def test_the_sweep_resends_a_crashed_jobs_message_once_its_lease_is_overdue(
    app_table: Table, job_queues: JobQueues
) -> None:
    clock = ShiftedClock()
    q = make(app_table, job_queues, clock=clock, sweep_grace_seconds=60)
    await q.enqueue("k.test", {}, dedupe_key="d", max_attempts=3)
    jid = await job_id_of(app_table, "d")
    assert await q.claim("crashed", 30) is not None
    clock.offset = timedelta(seconds=80)
    assert await q.sweep() == 0  # lease + grace not yet passed
    clock.offset = timedelta(seconds=91)
    assert await q.sweep() == 1
    assert messages(job_queues) == 2  # the crashed worker's (in flight) and the re-send
    job = await q.claim("w2", 30)  # the app clock says the lease expired: reclaimed
    assert job is not None and job.id == jid and job.attempts == 2


async def test_a_poison_message_goes_to_the_dlq_after_max_receive_count(
    app_table: Table, floci: str
) -> None:
    from tests.support.dynamo import local_clients

    queues = create_job_queues(local_clients(floci).sqs, max_receive_count=2)
    try:
        q = make(app_table, queues)
        send(queues, "not a job message")
        send(queues, json.dumps({"job_id": str(uuid.uuid4())}))  # names no job
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while messages(queues, queues.dlq_url) < 2:
            assert loop.time() < deadline, "poison messages never reached the DLQ"
            assert await q.claim("w", 1) is None
        assert messages(queues) == 0
    finally:
        delete_job_queues(queues)


async def test_the_worker_relays_claims_and_completes_an_outbox_job(
    app_table: Table, job_queues: JobQueues
) -> None:
    q = make(app_table, job_queues)
    jid = await _outbox_only(app_table, "from-the-api")
    done = asyncio.Event()
    seen: list[Job] = []

    async def handler(job: Job) -> None:
        seen.append(job)
        done.set()

    w = Worker(
        q, {"k.test": handler}, lease_seconds=30, heartbeat_seconds=10, poll_seconds=0.2,
        relay_seconds=0.2,
    )  # fmt: skip
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.wait_for(done.wait(), 15)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while (await item(app_table, jid))["status"] != "SUCCEEDED":
        assert loop.time() < deadline
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert [j.id for j in seen] == [jid] and messages(job_queues) == 0
