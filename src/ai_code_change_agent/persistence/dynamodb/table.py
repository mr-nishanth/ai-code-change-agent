"""Async facade over one DynamoDB table (boto3 low-level client, calls run in `asyncio.to_thread`).

Items go in and come out as plain Python values (boto3's type (de)serializer: numbers come back as
`Decimal`, floats are refused); `codec` turns them into domain values. Reads on the base table are
strongly consistent unless the caller asks otherwise; index reads are always eventual (DynamoDB
rejects `ConsistentRead` on a GSI). `query` follows `LastEvaluatedKey` until the result is complete
or `limit` items are collected (`Limit` counts items before a filter, so filtered queries loop).
Every failure is mapped by `errors`: no botocore text leaves this package.

A transaction retried after a cancellation (`TransactionConflict`, throttling) is sent with a
fresh `ClientRequestToken`: the cancelled attempt wrote nothing, and AWS semantics for reusing the
token of a cancelled request are not something the adapter relies on. botocore's own retries of one
HTTP request (timeouts, 5xx) keep the token, which makes them idempotent."""

import asyncio
import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer  # type: ignore[import-untyped]
from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from ai_code_change_agent.persistence.dynamodb.errors import (
    Classified,
    Failure,
    Item,
    RetryPolicy,
    classify,
    resolve_failure,
    terminal_error,
    transport_error,
)
from ai_code_change_agent.persistence.dynamodb.keys import Key

if TYPE_CHECKING:
    from ai_code_change_agent.persistence.dynamodb.transactions import TxBuilder

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()


def serialize_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _SERIALIZER.serialize(value) for name, value in item.items()}


def deserialize_item(raw: Mapping[str, Any] | None) -> Item | None:
    if raw is None:
        return None
    return {name: _DESERIALIZER.deserialize(value) for name, value in raw.items()}


def expression_args(
    *,
    condition: str | None = None,
    names: Mapping[str, str] | None = None,
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if condition:
        args["ConditionExpression"] = condition
    if names:
        args["ExpressionAttributeNames"] = dict(names)
    if values:
        args["ExpressionAttributeValues"] = serialize_item(values)
    return args


def _failure_args(failure: Failure | None) -> dict[str, Any]:
    # The old image is requested only when a caller wants to look at it.
    if failure is not None and not isinstance(failure, BaseException):
        return {"ReturnValuesOnConditionCheckFailure": "ALL_OLD"}
    return {}


class Table:
    def __init__(self, client: Any, name: str, *, retry: RetryPolicy | None = None) -> None:
        self._client = client
        self.name = name
        self.retry = retry or RetryPolicy()

    def transaction(self, token: str | None = None) -> "TxBuilder":
        from ai_code_change_agent.persistence.dynamodb.transactions import TxBuilder

        return TxBuilder(self, token)

    async def get(self, key: Key, *, consistent: bool = True) -> Item | None:
        response = await self._call(
            "get_item",
            {
                "TableName": self.name,
                "Key": serialize_item(key.as_dict()),
                "ConsistentRead": consistent,
            },
        )
        return deserialize_item(response.get("Item"))

    async def put(
        self,
        item: Mapping[str, Any],
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> None:
        request = {
            "TableName": self.name,
            "Item": serialize_item(item),
            **expression_args(condition=condition, names=names, values=values),
            **_failure_args(on_failed),
        }
        await self._call("put_item", request, _single(on_failed))

    async def update(
        self,
        key: Key,
        *,
        update: str,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
        return_values: Literal["ALL_NEW", "ALL_OLD"] = "ALL_NEW",
    ) -> Item:
        """Apply an update expression; returns the item after (`ALL_NEW`, the default) or before
        (`ALL_OLD`, empty when the item did not exist) the update."""
        request = {
            "TableName": self.name,
            "Key": serialize_item(key.as_dict()),
            "UpdateExpression": update,
            "ReturnValues": return_values,
            **expression_args(condition=condition, names=names, values=values),
            **_failure_args(on_failed),
        }
        response = await self._call("update_item", request, _single(on_failed))
        return deserialize_item(response.get("Attributes")) or {}

    async def delete(
        self,
        key: Key,
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        on_failed: Failure | None = None,
    ) -> None:
        request = {
            "TableName": self.name,
            "Key": serialize_item(key.as_dict()),
            **expression_args(condition=condition, names=names, values=values),
            **_failure_args(on_failed),
        }
        await self._call("delete_item", request, _single(on_failed))

    async def query(
        self,
        *,
        key_condition: str,
        values: Mapping[str, Any],
        names: Mapping[str, str] | None = None,
        index: str | None = None,
        consistent: bool | None = None,
        filter_expression: str | None = None,
        forward: bool = True,
        limit: int | None = None,
        page_size: int | None = None,
    ) -> list[Item]:
        """All matching items (or the first `limit`), following `LastEvaluatedKey`."""
        if consistent is None:
            consistent = index is None
        if consistent and index is not None:
            raise ValueError("index reads cannot be strongly consistent")
        request: dict[str, Any] = {
            "TableName": self.name,
            "KeyConditionExpression": key_condition,
            "ExpressionAttributeValues": serialize_item(values),
            "ScanIndexForward": forward,
            "ConsistentRead": consistent,
        }
        if index is not None:
            request["IndexName"] = index
        if names:
            request["ExpressionAttributeNames"] = dict(names)
        if filter_expression:
            request["FilterExpression"] = filter_expression
        if page_size is not None:
            request["Limit"] = page_size
        items: list[Item] = []
        while True:
            response = await self._call("query", request)
            items.extend(deserialize_item(raw) or {} for raw in response.get("Items", []))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            request["ExclusiveStartKey"] = last

    async def transact_write(self, tx: "TxBuilder") -> None:
        request = tx.request()

        def on_condition(classified: Classified) -> BaseException:
            failure = tx.failure_at(classified.index) if classified.index is not None else None
            return resolve_failure(failure, deserialize_item(classified.old_item))

        def fresh_token(retry_request: dict[str, Any]) -> None:
            retry_request["ClientRequestToken"] = str(uuid.uuid4())

        await self._call("transact_write_items", request, on_condition, fresh_token)

    async def _call(
        self,
        operation: str,
        request: dict[str, Any],
        on_condition: Callable[[Classified], BaseException] | None = None,
        before_retry: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        method = getattr(self._client, operation)
        for retry in range(self.retry.attempts):
            classified: Classified | None = None
            failure: BaseException | None = None
            try:
                response: dict[str, Any] = await asyncio.to_thread(method, **request)
                return response
            except ClientError as exc:
                classified = classify(exc)
            except BotoCoreError as exc:
                failure = transport_error(exc)
            # Raised outside the except blocks so no botocore exception is attached as context.
            if failure is not None:
                raise failure
            assert classified is not None  # noqa: S101
            if classified.is_condition:
                raise (on_condition or _single(None))(classified)
            if not classified.retryable or retry + 1 >= self.retry.attempts:
                raise terminal_error(classified)
            await self.retry.sleep(self.retry.delay(retry))
            if before_retry is not None:
                before_retry(request)
        raise AssertionError("unreachable")  # pragma: no cover


def _single(failure: Failure | None) -> Callable[[Classified], BaseException]:
    def on_condition(classified: Classified) -> BaseException:
        return resolve_failure(failure, deserialize_item(classified.old_item))

    return on_condition
