"""Usage ledger item encoding and `Table.batch_get`'s rare paths (botocore Stubber, no network)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from botocore.stub import Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import (
    CorruptItemError,
    PersistenceError,
    RetryPolicy,
    Table,
    keys,
)
from ai_code_change_agent.persistence.dynamodb.usage_store import _record, usage_item

NOW = datetime(2026, 11, 27, 9, 29, 17, 162000, tzinfo=UTC)


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def stubbed() -> Iterator[tuple[Table, Stubber]]:
    aws = AwsClients(Settings(_env_file=None, app_env="test"))
    policy = RetryPolicy(attempts=2, sleep=_no_sleep, random=lambda: 0.0)
    with Stubber(aws.dynamodb) as stub:
        yield Table(aws.dynamodb, "acca-test", retry=policy), stub
        stub.assert_no_pending_responses()


def _key(pk: str) -> dict[str, dict[str, str]]:
    return {"PK": {"S": pk}, "SK": {"S": "X"}}


async def test_unprocessed_keys_are_sent_again(stubbed: tuple[Table, Stubber]) -> None:
    table, stub = stubbed
    stub.add_response(
        "batch_get_item",
        {
            "Responses": {"acca-test": [{**_key("A"), "n": {"N": "1"}}]},
            "UnprocessedKeys": {"acca-test": {"Keys": [_key("B")], "ConsistentRead": True}},
        },
        {"RequestItems": {"acca-test": {"Keys": [_key("A"), _key("B")], "ConsistentRead": True}}},
    )
    stub.add_response(
        "batch_get_item",
        {"Responses": {"acca-test": [{**_key("B"), "n": {"N": "2"}}]}, "UnprocessedKeys": {}},
        {"RequestItems": {"acca-test": {"Keys": [_key("B")], "ConsistentRead": True}}},
    )
    found = await table.batch_get([keys.Key("A", "X"), keys.Key("B", "X")])
    assert {k.pk: v["n"] for k, v in found.items()} == {"A": Decimal(1), "B": Decimal(2)}


async def test_keys_still_unprocessed_after_the_retries_fail(
    stubbed: tuple[Table, Stubber],
) -> None:
    table, stub = stubbed
    unprocessed = {"acca-test": {"Keys": [_key("A")], "ConsistentRead": True}}
    for _ in range(2):
        stub.add_response(
            "batch_get_item", {"Responses": {"acca-test": []}, "UnprocessedKeys": unprocessed}
        )
    with pytest.raises(PersistenceError, match="batch_get_unprocessed_keys"):
        await table.batch_get([keys.Key("A", "X")])


def _call(**over: object) -> LlmCallRecord:
    fields: dict[str, object] = {
        "call_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "attempt_id": None,
        "stage": "s" * 40,
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "succeeded": True,
        "usage": TokenUsage(1, 2, 3, 4, 5),
        "cost_usd": Decimal("0.00000000005"),
        "pricing_version": "v",
        "created_at": NOW,
    }
    fields.update(over)
    return LlmCallRecord(**fields)  # type: ignore[arg-type]


def test_items_round_trip_with_sql_column_limits_and_rounding() -> None:
    record = _call()
    item = usage_item(record, keys.usage_session_pk(str(record.session_id)))
    assert item["cost_usd"] == "0.0000000001"  # half-up at the tenth place, like Numeric(16, 10)
    assert "attempt_id" not in item and "provider_message_id" not in item
    back = _record(item)
    assert back.stage == "s" * 32 and back.usage == TokenUsage(1, 2, 3, 4, 5)
    assert back.cost_usd == Decimal("1E-10") and back.created_at == NOW
    unknown = usage_item(_call(usage=None, cost_usd=None, succeeded=False), "USAGE#SES#x")
    assert "input_tokens" not in unknown and "cost_usd" not in unknown
    assert _record(unknown).usage is None and _record(unknown).cost_usd is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("cost_usd", "not-a-number"), ("succeeded", "yes"), ("stage", 3), ("input_tokens", "1")],
)
def test_a_corrupt_item_names_the_attribute_only(name: str, value: object) -> None:
    item = usage_item(_call(), "USAGE#SES#x") | {name: value}
    with pytest.raises(CorruptItemError, match=f"corrupt_item:{name}$"):
        _record(item)
