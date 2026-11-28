"""DynamoDB request counts on the hot paths (owner request: avoid over-cost), against Floci.

Every DynamoDB request is counted with a botocore `before-call` hook on the table's client, so a
change that adds a read or a write to webhook intake, a job claim, an attempt transition or an idle
dispatcher tick fails here. The largest hot-path item is measured with the DynamoDB item-size rule
(attribute name bytes + value bytes) and must stay within one 1 KB write unit."""

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.domain.changes.states import AttemptStatus as S
from ai_code_change_agent.domain.webhooks.models import Outcome
from ai_code_change_agent.persistence.dynamodb import Table
from ai_code_change_agent.persistence.dynamodb.change_store import (
    AttemptPointers,
    DynamoChangeUnitOfWork,
)
from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
from ai_code_change_agent.persistence.dynamodb.webhook_repository import DynamoWebhookRepository
from tests.integration.floci.test_change_store_dynamo import BASE, seed_request
from tests.integration.floci.test_webhook_dynamo import accepted, items
from tests.support.dynamo import JobQueues

pytestmark = [pytest.mark.floci, pytest.mark.integration]
WRITE_UNIT_BYTES = 1024


class Calls:
    """The DynamoDB and SQS operations sent through the hooked clients, in order."""

    def __init__(self, *clients: Any) -> None:
        self.seen: list[str] = []
        for client in clients:
            client.meta.events.register("before-call", self._record)

    def _record(self, model: Any, **_: Any) -> None:
        self.seen.append(model.name)

    def take(self) -> list[str]:
        taken, self.seen = self.seen, []
        return taken


@pytest.fixture
def calls(app_table: Table, job_queues: JobQueues) -> Iterator[Calls]:
    yield Calls(app_table._client, job_queues.sqs)


def _size(value: Any) -> int:
    """DynamoDB's item-size rule (approximate for numbers: about one byte per two digits)."""
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, int | Decimal):
        return len(str(value)) // 2 + 2
    if isinstance(value, bytes | bytearray):
        return len(value)
    if isinstance(value, dict):
        return 3 + sum(len(k.encode()) + _size(v) + 1 for k, v in value.items())
    if isinstance(value, list):
        return 3 + sum(_size(v) + 1 for v in value)
    raise TypeError(type(value).__name__)


def item_size(item: dict[str, Any]) -> int:
    return sum(len(name.encode()) + _size(value) for name, value in item.items())


async def test_webhook_intake_is_one_transaction(app_table: Table, calls: Calls) -> None:
    repo = DynamoWebhookRepository(app_table)
    assert await repo.record(accepted()) is Outcome.ACCEPTED
    assert calls.take() == ["TransactWriteItems"]
    assert await repo.record(accepted()) is Outcome.DUPLICATE  # a redelivery: still one request
    assert calls.take() == ["TransactWriteItems"]


async def test_a_job_claim_is_one_receive_and_one_conditional_update(
    app_table: Table, job_queues: JobQueues, calls: Calls
) -> None:
    queue = DynamoSqsJobQueue(app_table, job_queues.sqs, job_queues.url, receive_wait_seconds=1)
    assert await queue.enqueue("k.test", {"n": 1}, dedupe_key="calls-1", max_attempts=3)
    assert calls.take() == ["TransactWriteItems", "SendMessage", "UpdateItem"]
    job = await queue.claim("w1", 60)
    assert job is not None
    assert calls.take() == ["ReceiveMessage", "UpdateItem"]
    assert await queue.heartbeat(job.id, "w1", 60)
    assert calls.take() == ["UpdateItem", "ChangeMessageVisibility"]
    assert await queue.complete(job.id, "w1")
    assert calls.take() == ["UpdateItem", "DeleteMessage"]


async def test_idle_dispatcher_ticks_are_one_index_query_each(
    app_table: Table, job_queues: JobQueues, calls: Calls
) -> None:
    queue = DynamoSqsJobQueue(app_table, job_queues.sqs, job_queues.url, receive_wait_seconds=0)
    assert await queue.dispatch_pending() == 0
    assert await queue.sweep() == 0
    assert calls.take() == ["Query", "Query"]


async def test_an_attempt_transition_is_three_reads_and_one_transaction(
    app_table: Table, calls: Calls
) -> None:
    pointers = AttemptPointers()
    svc = CodeChangeService(lambda: DynamoChangeUnitOfWork(app_table, pointers=pointers))
    rid = await seed_request(app_table, repository_id=uuid.uuid4())
    aid = (await svc.start_attempt(rid)).id
    calls.take()
    # attempt (consistent GetItem), its approvals (consistent Query on APR#<aid>#), the request
    # (consistent GetItem for the status projection), then one TransactWriteItems; the
    # ATTEMPT#<aid>/PTR pointer is read once per process and cached.
    await svc.transition(aid, S.PREPARING)
    assert calls.take() == ["GetItem", "GetItem", "Query", "GetItem", "TransactWriteItems"]
    await svc.pin_baseline(aid, BASE)
    assert calls.take() == ["GetItem", "Query", "GetItem", "TransactWriteItems"]
    # The studio list: GSI2 keys (KEYS_ONLY for attempts), then one eventual BatchGetItem.
    assert [x.attempt.id for x in await svc.recent_attempts(10)] == [aid]
    assert calls.take() == ["Query", "BatchGetItem"]


async def test_hot_path_items_fit_one_write_unit(app_table: Table, job_queues: JobQueues) -> None:
    assert await DynamoWebhookRepository(app_table).record(accepted()) is Outcome.ACCEPTED
    svc = CodeChangeService(lambda: DynamoChangeUnitOfWork(app_table, pointers=AttemptPointers()))
    aid = (await svc.start_attempt(await seed_request(app_table, repository_id=uuid.uuid4()))).id
    await svc.transition(aid, S.PREPARING)
    await svc.pin_baseline(aid, BASE)
    sizes = {str(i["entity"]): 0 for i in items(app_table)}
    for item in items(app_table):
        sizes[str(item["entity"])] = max(sizes[str(item["entity"])], item_size(item))
    hot = {k: v for k, v in sizes.items() if k in {"code_change_request", "attempt", "job"}}
    assert set(hot) == {"code_change_request", "attempt", "job"}
    print(f"hot-path item sizes (bytes): {sizes}")  # reported by Task 13b (pytest -s)
    assert max(hot.values()) <= WRITE_UNIT_BYTES
