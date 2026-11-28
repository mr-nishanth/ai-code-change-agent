"""Error mapping and retry with botocore Stubber: conflicts and throttling never happen on Floci
(DYNAMODB-DATA-MODEL.md 10.4), so these paths are proven here. No request leaves the process."""

from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError  # type: ignore[import-untyped]
from botocore.stub import Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.domain.changes.errors import (
    ChangeConflictError,
    ConcurrentModificationError,
)
from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import (
    Key,
    PersistenceError,
    RetryPolicy,
    Table,
    TransactionTooLargeError,
)
from ai_code_change_agent.persistence.dynamodb.transactions import TxBuilder

SECRET = "driver detail: arn:aws:dynamodb:us-east-1:123456789012:table/acca token=abc"


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def stubbed(sleeps: Sleeps) -> Iterator[tuple[Table, Stubber]]:
    aws = AwsClients(Settings(_env_file=None, app_env="test"))
    policy = RetryPolicy(
        attempts=3, base_delay=0.1, max_delay=1.0, sleep=sleeps, random=lambda: 1.0
    )
    with Stubber(aws.dynamodb) as stub:
        yield Table(aws.dynamodb, "acca-test", retry=policy), stub
        stub.assert_no_pending_responses()


def cancelled(stub: Stubber, *codes: str, item: dict[str, Any] | None = None) -> None:
    reasons: list[dict[str, Any]] = [{"Code": code, "Message": SECRET} for code in codes]
    if item is not None:
        reasons[codes.index("ConditionalCheckFailed")]["Item"] = item
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message=SECRET,
        http_status_code=400,
        modeled_fields={"CancellationReasons": reasons},
    )


def failed(stub: Stubber, operation: str, code: str, **modeled: Any) -> None:
    stub.add_client_error(
        operation,
        service_error_code=code,
        service_message=SECRET,
        http_status_code=400,
        modeled_fields=modeled or None,
    )


def two_puts(table: Table, **kw: Any) -> TxBuilder:
    tx = table.transaction("token-1")
    tx.put({"PK": "A", "SK": "X"})
    tx.put({"PK": "B", "SK": "X"}, condition="attribute_not_exists(PK)", **kw)
    return tx


def assert_clean(exc: BaseException) -> None:
    assert SECRET not in str(exc) and "123456789012" not in repr(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def test_concurrent_modification_is_both_conflict_errors() -> None:
    error = ConcurrentModificationError()
    assert isinstance(error, ChangeConflictError)
    assert isinstance(error, ConflictError)
    assert error.code == "concurrent_modification"
    assert str(error) == "concurrent_modification"


async def test_transaction_conflict_is_retried_with_a_fresh_token(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    tokens: list[str] = []
    table._client.meta.events.register(
        "provide-client-params.dynamodb.TransactWriteItems",
        lambda params, **_: tokens.append(params["ClientRequestToken"]),
    )
    tx = two_puts(table)
    cancelled(stub, "None", "TransactionConflict")
    cancelled(stub, "ThrottlingError", "None")
    stub.add_response("transact_write_items", {})

    await tx.commit()

    assert sleeps.delays == [0.1, 0.2]
    # a cancelled transaction wrote nothing: each retry is a new request with its own token
    assert tokens[0] == "token-1" and len(set(tokens)) == 3
    assert all(len(token) == 36 for token in tokens[1:])
    assert tx.request()["ClientRequestToken"] == "token-1"


async def test_transaction_conflict_surfaces_as_concurrent_modification(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    for _ in range(3):
        cancelled(stub, "TransactionConflict", "None")

    with pytest.raises(ConcurrentModificationError) as raised:
        await two_puts(table).commit()

    assert_clean(raised.value)
    assert sleeps.delays == [0.1, 0.2]  # bounded: 3 attempts, 2 sleeps


async def test_throttling_is_retried_then_surfaces_as_persistence_error(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    failed(stub, "put_item", "ThrottlingException")
    failed(stub, "put_item", "ProvisionedThroughputExceededException")
    failed(stub, "put_item", "ThrottlingException")

    with pytest.raises(PersistenceError) as raised:
        await table.put({"PK": "A", "SK": "X"})

    assert str(raised.value) == "ThrottlingException"
    assert not isinstance(raised.value, ConcurrentModificationError)
    assert_clean(raised.value)
    assert len(sleeps.delays) == 2


async def test_throttling_reasons_in_a_cancelled_transaction_are_retried(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    cancelled(stub, "ThrottlingError", "None")
    cancelled(stub, "None", "ProvisionedThroughputExceeded")
    stub.add_response("transact_write_items", {})

    await two_puts(table).commit()
    assert len(sleeps.delays) == 2


async def test_single_item_transaction_conflict_and_throttled_reads_are_retried(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    failed(stub, "update_item", "TransactionConflictException")
    stub.add_response(
        "update_item", {"Attributes": {"PK": {"S": "A"}, "SK": {"S": "X"}, "n": {"N": "2"}}}
    )
    failed(stub, "get_item", "ProvisionedThroughputExceededException")
    stub.add_response(
        "get_item",
        {"Item": {"PK": {"S": "A"}, "SK": {"S": "X"}}},
        {
            "TableName": "acca-test",
            "Key": {"PK": {"S": "A"}, "SK": {"S": "X"}},
            "ConsistentRead": True,
        },
    )
    for _ in range(3):
        failed(stub, "update_item", "TransactionConflictException")

    assert await table.update(Key("A", "X"), update="ADD n :one", values={":one": 1}) == {
        "PK": "A",
        "SK": "X",
        "n": 2,
    }
    assert await table.get(Key("A", "X")) == {"PK": "A", "SK": "X"}
    with pytest.raises(ConcurrentModificationError) as raised:
        await table.update(Key("A", "X"), update="ADD n :one", values={":one": 1})
    assert_clean(raised.value)
    assert len(sleeps.delays) == 4


async def test_condition_failures_map_by_item_index_and_are_not_retried(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps
) -> None:
    table, stub = stubbed
    cancelled(stub, "None", "ConditionalCheckFailed")
    with pytest.raises(ConflictError, match=r"^alias_taken$"):
        await two_puts(table, on_failed=ConflictError("alias_taken")).commit()

    seen: list[Any] = []
    cancelled(
        stub, "None", "ConditionalCheckFailed", item={"PK": {"S": "B"}, "request_id": {"S": "r-1"}}
    )

    def duplicate(old: dict[str, Any] | None) -> ConflictError:
        seen.append(old)
        return ConflictError("duplicate")

    tx = two_puts(table, on_failed=duplicate)
    assert (
        tx.request()["TransactItems"][1]["Put"]["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"
    )
    with pytest.raises(ConflictError, match=r"^duplicate$"):
        await tx.commit()
    assert seen == [{"PK": "B", "request_id": "r-1"}]

    cancelled(stub, "ConditionalCheckFailed", "None")
    with pytest.raises(ConcurrentModificationError) as raised:  # item 0 has no mapping
        await two_puts(table, on_failed=ConflictError("alias_taken")).commit()
    assert_clean(raised.value)

    failed(stub, "put_item", "ConditionalCheckFailedException")
    with pytest.raises(ConflictError, match=r"^exists$"):
        await table.put(
            {"PK": "A", "SK": "X"},
            condition="attribute_not_exists(PK)",
            on_failed=ConflictError("exists"),
        )
    failed(
        stub,
        "delete_item",
        "ConditionalCheckFailedException",
        Item={"PK": {"S": "A"}, "SK": {"S": "X"}},
    )
    olds: list[Any] = []

    def gone(old: dict[str, Any] | None) -> ConflictError:
        olds.append(old)
        return ConflictError("gone")

    with pytest.raises(ConflictError):
        await table.delete(Key("A", "X"), condition="attribute_exists(PK)", on_failed=gone)
    assert olds == [{"PK": "A", "SK": "X"}]
    assert sleeps.delays == []


@pytest.mark.parametrize(
    ("operation", "code", "expected"),
    [
        ("put_item", "ValidationException", "ValidationException"),
        ("put_item", "ResourceNotFoundException", "ResourceNotFoundException"),
        (
            "transact_write_items",
            "IdempotentParameterMismatchException",
            "IdempotentParameterMismatchException",
        ),
    ],
)
async def test_other_errors_are_not_retried_and_carry_only_the_code(
    stubbed: tuple[Table, Stubber], sleeps: Sleeps, operation: str, code: str, expected: str
) -> None:
    table, stub = stubbed
    failed(stub, operation, code)
    with pytest.raises(PersistenceError) as raised:
        if operation == "put_item":
            await table.put({"PK": "A", "SK": "X"})
        else:
            await two_puts(table).commit()
    assert str(raised.value) == expected
    assert_clean(raised.value)
    assert sleeps.delays == []


async def test_cancellation_for_other_reasons_names_the_reason_only(
    stubbed: tuple[Table, Stubber],
) -> None:
    table, stub = stubbed
    cancelled(stub, "None", "ValidationError")
    with pytest.raises(PersistenceError) as raised:
        await two_puts(table).commit()
    assert str(raised.value) == "TransactionCanceledException:ValidationError"
    cancelled(stub, "None", "None")
    with pytest.raises(PersistenceError) as raised:
        await two_puts(table).commit()
    assert str(raised.value) == "TransactionCanceledException"
    assert_clean(raised.value)


class _Broken:
    def get_item(self, **_: Any) -> Any:
        raise EndpointConnectionError(endpoint_url="http://127.0.0.1:1/" + SECRET)


async def test_transport_errors_carry_only_the_class_name() -> None:
    with pytest.raises(PersistenceError) as raised:
        await Table(_Broken(), "t").get(Key("A", "X"))
    assert str(raised.value) == "EndpointConnectionError"
    assert_clean(raised.value)


def test_transactions_are_refused_before_the_call_when_invalid(
    stubbed: tuple[Table, Stubber],
) -> None:
    table, _ = stubbed  # Stubber fails the test on any unexpected call
    tx = table.transaction()
    for n in range(100):
        tx.condition_check(Key(f"K{n}", "X"), condition="attribute_exists(PK)")
    with pytest.raises(TransactionTooLargeError) as raised:
        tx.delete(Key("K100", "X"))
    assert str(raised.value) == "transaction_too_large"
    assert len(tx) == 100 and tx.failure_at(100) is None and tx.failure_at(-1) is None
    with pytest.raises(ValueError, match="one operation per item"):
        table.transaction().put({"PK": "A", "SK": "X"}).update(
            Key("A", "X"), update="SET a = :a", values={":a": 1}
        )
    with pytest.raises(ValueError, match="empty"):
        table.transaction().request()
    with pytest.raises(ValueError, match="ClientRequestToken"):
        table.transaction("x" * 37)
    assert len(table.transaction().token) == 36


def test_retry_policy_is_bounded_with_full_jitter() -> None:
    policy = RetryPolicy(base_delay=0.1, max_delay=0.3, random=lambda: 1.0)
    assert [policy.delay(n) for n in range(4)] == [0.1, 0.2, 0.3, 0.3]
    assert RetryPolicy(random=lambda: 0.0).delay(3) == 0.0
    assert 0.0 <= RetryPolicy().delay(10) <= RetryPolicy().max_delay
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)


async def test_index_reads_cannot_be_consistent(stubbed: tuple[Table, Stubber]) -> None:
    table, _ = stubbed
    with pytest.raises(ValueError, match="consistent"):
        await table.query(
            index="GSI1", key_condition="GSI1PK = :p", values={":p": "x"}, consistent=True
        )


async def test_default_sleep_and_clock() -> None:
    from datetime import UTC

    from ai_code_change_agent.persistence.dynamodb import SystemClock
    from ai_code_change_agent.persistence.dynamodb.errors import _default_sleep

    await _default_sleep(0)
    assert SystemClock().now().tzinfo is UTC
