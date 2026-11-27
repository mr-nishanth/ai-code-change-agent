"""DynamoDB foundations against Floci: transactions, idempotency tokens, optimistic locking, the
unit-of-work base and query pagination. Each test runs on its own `s2116-<uuid>` table."""

import asyncio
import uuid
from typing import Any

import pytest

from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.persistence.dynamodb import (
    DynamoUnitOfWork,
    Key,
    PersistenceError,
    Table,
    TransactionTooLargeError,
)

pytestmark = [pytest.mark.floci, pytest.mark.integration]


def item(pk: str, sk: str = "X", **attrs: Any) -> dict[str, Any]:
    return {"PK": pk, "SK": sk, **attrs}


def count_calls(table: Table, operation: str) -> list[str]:
    calls: list[str] = []
    table._client.meta.events.register(
        f"before-call.dynamodb.{operation}", lambda **_: calls.append(operation)
    )
    return calls


async def test_cancellation_at_index_2_of_3_writes_nothing(app_table: Table) -> None:
    tx = app_table.transaction()
    tx.put(item("A"))
    tx.put(item("B"))
    tx.update(
        Key("C", "X"),
        update="SET v = :one",
        condition="attribute_exists(PK)",
        values={":one": 1},
        on_failed=ConflictError("c_missing"),
    )

    with pytest.raises(ConflictError, match=r"^c_missing$"):
        await tx.commit()

    for pk in ("A", "B", "C"):
        assert await app_table.get(Key(pk, "X")) is None


async def test_failed_item_reports_its_old_image(app_table: Table) -> None:
    await app_table.put(item("GUARD", request_id="r-1"))
    seen: list[Any] = []

    def duplicate(old: dict[str, Any] | None) -> ConflictError:
        seen.append(old)
        return ConflictError("duplicate")

    tx = app_table.transaction()
    tx.put(item("NEW"))
    tx.put(
        item("GUARD", request_id="r-2"), condition="attribute_not_exists(PK)", on_failed=duplicate
    )

    with pytest.raises(ConflictError):
        await tx.commit()
    assert seen == [{"PK": "GUARD", "SK": "X", "request_id": "r-1"}]
    assert await app_table.get(Key("NEW", "X")) is None


async def test_a_token_replay_applies_once(app_table: Table) -> None:
    token = str(uuid.uuid4())
    tx = app_table.transaction(token)
    tx.update(Key("CTR", "X"), update="ADD n :one", values={":one": 1})
    tx.put(item("LOG", entry="once"))

    await tx.commit()
    await tx.commit()  # the same commit sent again, e.g. after a lost response

    counter = await app_table.get(Key("CTR", "X"))
    assert counter is not None and counter["n"] == 1
    other = app_table.transaction(token)
    other.update(Key("CTR", "X"), update="ADD n :two", values={":two": 2})
    with pytest.raises(PersistenceError) as raised:
        await other.commit()
    assert str(raised.value) == "IdempotentParameterMismatchException"
    counter = await app_table.get(Key("CTR", "X"))
    assert counter is not None and counter["n"] == 1


async def test_100_items_commit_and_the_101st_is_refused_before_any_call(
    app_table: Table,
) -> None:
    calls = count_calls(app_table, "TransactWriteItems")
    tx = app_table.transaction()
    for n in range(100):
        tx.put(item(f"BULK#{n:03d}"))
    with pytest.raises(TransactionTooLargeError):
        tx.put(item("BULK#100"))
    assert calls == []

    await tx.commit()
    assert calls == ["TransactWriteItems"]
    stored = await app_table.query(key_condition="PK = :pk", values={":pk": "BULK#099"})
    assert len(stored) == 1


class _Uow(DynamoUnitOfWork):
    pass


async def test_twelve_concurrent_version_cas_have_exactly_one_winner(app_table: Table) -> None:
    key = Key("REQ#cas", "REQUEST")
    await app_table.put(item(key.pk, key.sk, row_version=1, owner="nobody"))

    everyone_has_read = asyncio.Barrier(12)

    async def contender(n: int) -> str:
        async with _Uow(app_table) as uow:
            current = await uow.get(key)
            assert current is not None
            uow.replace({**current, "owner": f"worker-{n}"})
            await everyone_has_read.wait()  # all 12 decide on row_version 1
            try:
                await uow.commit()
            except ConcurrentModificationError:
                return "lost"
            return "won"

    results = await asyncio.gather(*(contender(n) for n in range(12)))

    assert sorted(results) == ["lost"] * 11 + ["won"]
    final = await app_table.get(key)
    assert final is not None and final["row_version"] == 2
    assert final["owner"].startswith("worker-")


async def test_unit_of_work_reads_its_own_writes_and_discards_without_commit(
    app_table: Table,
) -> None:
    async with _Uow(app_table) as uow:
        created = uow.create(item("REPO#1", "REPO", row_version=1, name="a"))
        assert await uow.get(Key("REPO#1", "REPO")) == created
        uow.create(item("REPO#1", "ALIAS#x"))
        bumped = uow.replace({**created, "name": "b"})
        assert bumped["row_version"] == 1  # one write per item: created at version 1
        assert [i["SK"] for i in await uow.query("REPO#1")] == ["ALIAS#x", "REPO"]
        assert [i["SK"] for i in await uow.query("REPO#1", "ALIAS#")] == ["ALIAS#x"]
        # leave without commit
    assert await app_table.query(key_condition="PK = :pk", values={":pk": "REPO#1"}) == []

    async with _Uow(app_table) as uow:
        uow.create(item("REPO#1", "REPO", row_version=1, name="a"))
        await uow.commit()
    async with _Uow(app_table) as uow:
        row = await uow.get(Key("REPO#1", "REPO"))
        assert row is not None
        assert uow.replace({**row, "name": "c"})["row_version"] == 2
        await uow.commit()
    stored = await app_table.get(Key("REPO#1", "REPO"))
    assert stored == item("REPO#1", "REPO", row_version=2, name="c")


async def test_commit_conditions_every_item_a_decision_read(app_table: Table) -> None:
    await app_table.put(item("PARENT", row_version=1))

    async with _Uow(app_table) as uow:
        assert await uow.get(Key("PARENT", "X")) is not None
        assert await uow.get(Key("GUARD", "X")) is None  # absent: must stay absent
        uow.create(item("CHILD"))
        # another writer changes what this unit of work based its decision on
        await app_table.put(item("PARENT", row_version=2))
        with pytest.raises(ConcurrentModificationError):
            await uow.commit()
    assert await app_table.get(Key("CHILD", "X")) is None

    async with _Uow(app_table) as uow:
        assert await uow.get(Key("GUARD", "X")) is None
        uow.create(item("CHILD"))
        await app_table.put(item("GUARD"))
        with pytest.raises(ConcurrentModificationError):
            await uow.commit()
    assert await app_table.get(Key("CHILD", "X")) is None

    async with _Uow(app_table) as uow:
        assert await uow.get(Key("PARENT", "X"), depend=False) is not None
        uow.create(item("CHILD"))
        await app_table.put(item("PARENT", row_version=3))
        await uow.commit()  # a display read is not a dependency
    assert await app_table.get(Key("CHILD", "X")) is not None


async def test_explicit_checks_and_guarded_creates_map_to_their_errors(
    app_table: Table,
) -> None:
    await app_table.put(item("PV#1", published=False))
    await app_table.put(item("ALIAS#taken"))

    async with _Uow(app_table) as uow:
        uow.create(item("REPO#2"))
        uow.check(
            Key("PV#1", "X"),
            condition="#p = :true",
            names={"#p": "published"},
            values={":true": True},
            on_failed=ConflictError("version_not_published"),
        )
        with pytest.raises(ConflictError, match=r"^version_not_published$"):
            await uow.commit()

    async with _Uow(app_table) as uow:
        uow.create(item("REPO#2"))
        uow.create(item("ALIAS#taken"), on_exists=ConflictError("alias_taken"))
        with pytest.raises(ConflictError, match=r"^alias_taken$"):
            await uow.commit()
    assert await app_table.get(Key("REPO#2", "X")) is None


async def test_deletes_are_version_or_existence_conditioned(app_table: Table) -> None:
    await app_table.put(item("JOB#1", row_version=4))
    await app_table.put(item("ACTIVE#1"))

    async with _Uow(app_table) as uow:
        assert await uow.get(Key("JOB#1", "X")) is not None
        assert await uow.get(Key("ACTIVE#1", "X")) is not None
        uow.delete(Key("JOB#1", "X"))
        uow.delete(Key("ACTIVE#1", "X"))
        assert await uow.get(Key("JOB#1", "X")) is None
        await uow.commit()
    assert await app_table.get(Key("JOB#1", "X")) is None
    assert await app_table.get(Key("ACTIVE#1", "X")) is None

    await app_table.put(item("JOB#2", row_version=1))
    async with _Uow(app_table) as uow:
        assert await uow.get(Key("JOB#2", "X")) is not None
        uow.delete(Key("JOB#2", "X"))
        await app_table.put(item("JOB#2", row_version=2))
        with pytest.raises(ConcurrentModificationError):
            await uow.commit()
    assert await app_table.get(Key("JOB#2", "X")) is not None


async def test_unit_of_work_refuses_ambiguous_writes(app_table: Table) -> None:
    await app_table.put(item("IMM", ptr="x"))  # immutable: no row_version
    await app_table.put(item("COLL", "A", row_version=1))
    async with _Uow(app_table) as uow:
        await uow.commit()  # nothing buffered: no call
        rows = await uow.query("COLL", depend=True)
        assert [r["SK"] for r in rows] == ["A"]
        immutable = await uow.get(Key("IMM", "X"))
        assert immutable is not None
        with pytest.raises(ValueError, match="versioned"):
            uow.replace(immutable)
        with pytest.raises(ValueError, match="read"):
            uow.delete(Key("NEVER", "X"))
        uow.create(item("NEW", row_version=1))
        with pytest.raises(ValueError, match="already written"):
            uow.create(item("NEW"))
        uow.delete(Key("NEW", "X"))  # created then deleted: nothing to send
        assert await uow.get(Key("NEW", "X")) is None
        uow.delete(Key("COLL", "A"))
        with pytest.raises(ValueError, match="deleted"):
            uow.replace(item("COLL", "A", row_version=1))
        with pytest.raises(ValueError, match="already written"):
            uow.delete(Key("COLL", "A"))
        assert await uow.query("COLL") == []
        await uow.commit()  # IMM (immutable) needs no check; COLL#A deleted
    assert await app_table.get(Key("COLL", "A")) is None
    assert await app_table.get(Key("NEW", "X")) is None


async def test_single_item_writes_and_conditions(app_table: Table) -> None:
    await app_table.put(item("D"), condition="attribute_not_exists(PK)")
    with pytest.raises(ConflictError, match=r"^duplicate$"):
        await app_table.put(
            item("D"), condition="attribute_not_exists(PK)", on_failed=ConflictError("duplicate")
        )
    with pytest.raises(ConcurrentModificationError):
        await app_table.update(
            Key("D", "X"), update="SET v = :v", condition="attribute_exists(v)", values={":v": 1}
        )
    updated = await app_table.update(Key("D", "X"), update="SET v = :v", values={":v": 7})
    assert updated == item("D", v=7)
    await app_table.delete(Key("D", "X"), condition="v = :v", values={":v": 7})
    assert await app_table.get(Key("D", "X")) is None


async def test_query_follows_last_evaluated_key_on_table_and_index(app_table: Table) -> None:
    tx = app_table.transaction()
    for n in range(30):
        tx.put(item("LIST", f"{n:02d}", GSI2PK="LIST#ATTEMPT", GSI2SK=f"{n:02d}", odd=n % 2))
    await tx.commit()

    everything = await app_table.query(
        key_condition="PK = :pk", values={":pk": "LIST"}, page_size=7
    )
    assert [i["SK"] for i in everything] == [f"{n:02d}" for n in range(30)]
    newest = await app_table.query(
        key_condition="PK = :pk", values={":pk": "LIST"}, forward=False, limit=3, page_size=2
    )
    assert [i["SK"] for i in newest] == ["29", "28", "27"]
    odd = await app_table.query(
        key_condition="PK = :pk",
        values={":pk": "LIST", ":one": 1},
        filter_expression="odd = :one",
        page_size=4,
    )
    assert len(odd) == 15
    indexed = await app_table.query(
        index="GSI2",
        key_condition="GSI2PK = :pk AND begins_with(GSI2SK, :p)",
        values={":pk": "LIST#ATTEMPT", ":p": "1"},
        page_size=3,
    )
    assert [i["GSI2SK"] for i in indexed] == [f"1{n}" for n in range(10)]
    with pytest.raises(ValueError, match="consistent"):
        await app_table.query(
            index="GSI2", key_condition="GSI2PK = :pk", values={":pk": "x"}, consistent=True
        )


async def test_oversized_items_fail_without_driver_text(app_table: Table) -> None:
    with pytest.raises(PersistenceError) as raised:
        await app_table.put(item("BIG", blob="x" * 410_000))
    assert str(raised.value) == "ValidationException"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
