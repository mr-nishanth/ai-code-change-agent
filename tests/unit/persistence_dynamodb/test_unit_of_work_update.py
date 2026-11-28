"""`DynamoUnitOfWork.update`: one targeted, guarded UpdateExpression on an item already read."""

from typing import Any

import pytest

from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.persistence.dynamodb import DynamoUnitOfWork, Key, Table

KEY = Key("REPO#1", "PV#000001")
DRAFT: dict[str, Any] = {"PK": KEY.pk, "SK": KEY.sk, "content_json": "{}"}


def uow_with_draft() -> DynamoUnitOfWork:
    uow = DynamoUnitOfWork(Table(client=None, name="t"))
    uow._read[KEY] = dict(DRAFT)
    return uow


def publish(uow: DynamoUnitOfWork, key: Key = KEY, **image: Any) -> dict[str, Any]:
    return uow.update(
        key,
        image={**DRAFT, "published_at": "now", **image},
        expression="SET published_at = :now",
        condition="attribute_not_exists(published_at)",
        values={":now": "now"},
        on_failed=ConflictError("publish_conflict"),
    )


async def test_update_is_buffered_seen_by_later_reads_and_sent_as_update() -> None:
    uow = uow_with_draft()
    assert not uow.is_written(KEY)

    publish(uow)

    assert uow.is_written(KEY)
    assert (await uow.get(KEY)) == {**DRAFT, "published_at": "now"}
    (entry,) = uow.build("token-1").request()["TransactItems"]
    body = entry["Update"]
    assert body["UpdateExpression"] == "SET published_at = :now"
    assert body["ConditionExpression"] == "attribute_not_exists(published_at)"
    assert body["ExpressionAttributeValues"] == {":now": {"S": "now"}}
    assert "Item" not in body  # content attributes are never sent


def test_update_needs_an_item_read_and_a_matching_image() -> None:
    uow = DynamoUnitOfWork(Table(client=None, name="t"))
    with pytest.raises(ValueError, match="needs an item read"):
        publish(uow)
    uow = uow_with_draft()
    with pytest.raises(ValueError, match="image does not match"):
        publish(uow, SK="PV#000002")


def test_one_operation_per_item_after_an_update() -> None:
    uow = uow_with_draft()
    publish(uow)
    with pytest.raises(ValueError, match="already written"):
        publish(uow)
    with pytest.raises(ValueError, match="updated in this unit of work"):
        uow.replace({**DRAFT, "row_version": 2})
