"""The DynamoDB usage ledger and cost queries against Floci (DYNAMODB-ACCESS-PATTERNS.md 6, U1-U5).

Ported from `tests/integration/test_usage_ledger_db.py` (which stays until PostgreSQL is removed):
idempotent, append-only inserts that survive a restart. The reports a worker prints are compared
with `golden/usage_reports.txt`, which the PostgreSQL store produced once from the same fixture
(`tests/support/cost_ledger.py`); the SQL path is not run here. Each test runs on its own
`s2116-<uuid>` table."""

import asyncio
import dataclasses
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.domain.changes.models import CodeChangeAttempt, CodeChangeRequest
from ai_code_change_agent.domain.repositories.models import NewRepository
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.change_store import (
    AttemptPointers,
    DynamoChangeUnitOfWork,
    request_item,
)
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore
from tests.support import cost_ledger as fx

pytestmark = [pytest.mark.floci, pytest.mark.integration]
GOLDEN = Path(__file__).parent / "golden" / "usage_reports.txt"


async def seed(table: Table) -> None:
    """The fixture's repositories, Stories and attempts, written by the real adapters."""
    repo_ids: dict[str, uuid.UUID] = {}
    for spec in fx.REPOS:
        async with DynamoRegistryUnitOfWork(table) as uow:
            repo = await uow.store.add_repository(
                NewRepository(
                    repository_key=spec.key,
                    display_name=spec.key,
                    git_provider="github",
                    provider_repository_id=spec.key,
                    owner="acme",
                    name=spec.key,
                    default_branch="main",
                    access_reference="ref",
                ),
                created_by="test",
            )
            await uow.commit()
        repo_ids[spec.key] = repo.id
    for req in fx.REQUESTS:
        request = CodeChangeRequest(req.id, req.jira_issue_id, req.issue_key, req.project_key)
        await table.put(request_item(request, created_at=fx.T0))
    for a in fx.ATTEMPTS:
        async with DynamoChangeUnitOfWork(table) as uow:
            await uow.store.add_attempt(
                CodeChangeAttempt(
                    id=a.id,
                    request_id=a.request.id,
                    number=a.number,
                    repository_id=repo_ids[a.repository],
                    profile_version_id=uuid.uuid4(),
                    status=a.status,
                    failure_code=a.failure_code,
                )
            )
            await uow.commit()


async def ledger(table: Table) -> DynamoUsageStore:
    await seed(table)
    store = DynamoUsageStore(table, pointers=AttemptPointers())
    for record in fx.RECORDS:
        assert await store.add(record)
    return store


async def all_items(table: Table) -> list[dict[str, object]]:
    response = await asyncio.to_thread(table._client.scan, TableName=table.name)
    return list(response["Items"])


def count_calls(table: Table, operation: str) -> list[str]:
    seen: list[str] = []
    table._client.meta.events.register(
        f"before-call.dynamodb.{operation}", lambda **_: seen.append(operation)
    )
    return seen


def call(msg: str | None, *, attempt: uuid.UUID | None = None) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=uuid.uuid4(),
        session_id=fx.SESSION_NOW,
        attempt_id=attempt,
        stage="planning",
        provider="anthropic",
        model="claude-haiku-5-5",
        succeeded=True,
        usage=TokenUsage(10, 5, 1, 2, 3),
        cost_usd=Decimal("0.0001234"),
        pricing_version="v",
        provider_message_id=msg,
    )


async def test_reports_match_the_sql_golden_text(app_table: Table) -> None:
    """task_report/session_summary (and the rows behind them) equal what PostgreSQL produced."""
    store = await ledger(app_table)
    assert await fx.render(store) == GOLDEN.read_text()
    # A fresh process (no pointer cache) reads the same ledger: nothing lived only in memory.
    restarted = DynamoUsageStore(app_table, pointers=AttemptPointers())
    assert await fx.render(restarted) == GOLDEN.read_text()


async def test_a_replayed_call_or_provider_message_records_nothing(app_table: Table) -> None:
    store = DynamoUsageStore(app_table, pointers=AttemptPointers())
    first = call("msg_1", attempt=fx.A12.id)
    assert await store.add(first)
    before = await all_items(app_table)
    assert len(before) == 4  # call guard, message guard, attempt copy, session copy
    assert not await store.add(first)  # same call id (a retried insert, a resumed checkpoint)
    replay = call("msg_1", attempt=fx.A11.id)  # new call id, same provider message
    assert not await store.add(replay)
    assert sorted(map(str, await all_items(app_table))) == sorted(map(str, before))
    assert [r.record.call_id for r in await store.rows(session_id=fx.SESSION_NOW)] == [
        first.call_id
    ]
    assert await store.rows(attempt_ids=[fx.A11.id]) == []
    # A failed call (no usage, no message id) is recorded, its cost unknown rather than zero.
    failed = LlmCallRecord(
        call_id=uuid.uuid4(),
        session_id=fx.SESSION_NOW,
        attempt_id=None,
        stage="planning",
        provider="anthropic",
        model="claude-haiku-5-5",
        succeeded=False,
        usage=None,
        cost_usd=None,
        pricing_version="v",
        error_code="APITimeoutError",
    )
    assert await store.add(failed)
    assert not await store.add(failed)
    rows = await DynamoUsageStore(app_table).rows(session_id=fx.SESSION_NOW)
    assert [r.record for r in rows] == [
        dataclasses.replace(first, cost_usd=Decimal("0.0001234000")),
        failed,
    ]
    assert rows[1].issue_key is None and rows[1].record.usage is None


async def test_one_transaction_writes_exact_decimal_strings(app_table: Table) -> None:
    store = DynamoUsageStore(app_table)
    record = next(r for r in fx.RECORDS if r.provider_message_id == "msg_a12_1")
    writes = count_calls(app_table, "TransactWriteItems")
    assert await store.add(record)
    assert writes == ["TransactWriteItems"]
    created = "2026-11-20T10:01:40.123456Z"
    copy = await app_table.get(
        keys.usage_attempt_copy(fx.A12.id, record.created_at, str(record.call_id))
    )
    assert copy is not None and copy["SK"] == f"{created}#{record.call_id}"
    assert copy["cost_usd"] == "0.0000000002"  # 1.5e-10 rounded half-up to ten places
    assert copy["input_tokens"] == Decimal(1500) and copy["cache_read_tokens"] == Decimal(1000)
    session_copy = await app_table.get(
        keys.usage_session_copy(str(fx.SESSION_NOW), record.created_at, str(record.call_id))
    )
    assert session_copy == copy | {"PK": keys.usage_session_pk(str(fx.SESSION_NOW))}
    big = next(r for r in fx.RECORDS if r.provider_message_id == "msg_a12_3")
    assert await store.add(big)
    stored = await app_table.get(
        keys.usage_session_copy(str(fx.SESSION_NOW), big.created_at, str(big.call_id))
    )
    assert stored is not None and stored["cost_usd"] == "1.2345678901"
    unpriced = next(r for r in fx.RECORDS if r.model == "mystery-model-1")
    assert await store.add(unpriced)
    raw = await app_table.get(
        keys.usage_session_copy(str(fx.SESSION_NOW), unpriced.created_at, str(unpriced.call_id))
    )
    assert raw is not None and "cost_usd" not in raw and raw["input_tokens"] == Decimal(2000)
    assert await app_table.get(keys.llm_call_guard(str(record.call_id))) is not None
    assert await app_table.get(keys.provider_message_guard("msg_a12_1")) is not None


async def test_joins_are_batched_and_read_once_per_call(app_table: Table) -> None:
    store = await ledger(app_table)
    gets = count_calls(app_table, "GetItem")
    batches = count_calls(app_table, "BatchGetItem")
    rows = await DynamoUsageStore(app_table, pointers=AttemptPointers()).rows(
        attempt_ids=[fx.A11.id, fx.A12.id, fx.A21.id, fx.A22.id, fx.A31.id]
    )
    assert len(rows) == 13 and gets == []
    assert len(batches) == 3  # pointers; attempts + requests; repositories
    batches.clear()
    await store.rows(issue_keys=["SHOP-1", "SHOP-2"])  # attempts and requests come from U3
    assert len(batches) == 1 and gets == []  # repositories only


async def test_a_row_whose_attempt_is_unknown_has_no_join_data(app_table: Table) -> None:
    store = DynamoUsageStore(app_table, pointers=AttemptPointers())
    orphan = call(None, attempt=uuid.uuid4())
    assert await store.add(orphan)
    [row] = await store.rows(attempt_ids=[orphan.attempt_id])  # type: ignore[list-item]
    assert (row.issue_key, row.attempt_number, row.attempt_status, row.repository) == (
        None,
        None,
        None,
        None,
    )
    assert await store.rows(issue_keys=["SHOP-1"]) == []


async def test_a_query_without_a_filter_is_refused(app_table: Table) -> None:
    store = DynamoUsageStore(app_table)
    with pytest.raises(ValueError, match="filter"):
        await store.rows()
    assert await store.rows(attempt_ids=[]) == [] and await store.rows(issue_keys=[]) == []


async def test_batch_get_reads_more_than_one_request_of_keys(app_table: Table) -> None:
    items = [{"PK": f"T#{i:03d}", "SK": "X", "n": i} for i in range(130)]
    for item in items:
        await app_table.put(item)
    wanted = [keys.Key(f"T#{i:03d}", "X") for i in range(140)]  # ten do not exist
    batches = count_calls(app_table, "BatchGetItem")
    found = await app_table.batch_get([*wanted, wanted[0]])
    assert len(batches) == 2 and len(found) == 130
    assert found[keys.Key("T#129", "X")]["n"] == Decimal(129)
    assert await app_table.batch_get([]) == {}
