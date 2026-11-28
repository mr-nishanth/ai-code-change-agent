"""The DynamoDB multi-repository store against Floci (DYNAMODB-ACCESS-PATTERNS.md 3, M1-M11).

Ported from the former PostgreSQL test `test_parent_report_db.py` (git history):
the durable Jira report lifecycle, now driven by an injected clock instead of rewriting
`jira_report_at` in SQL; plus the create, compare-and-set and attempt-link behaviour the SQL
constraints and index used to give. Each test runs on its own `s2116-<uuid>` table."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.application.jobs.queue import JobFailedError
from ai_code_change_agent.application.multirepo.coordinator import Coordinator
from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    Parent,
    ParentStatus,
    ReportState,
)
from ai_code_change_agent.domain.changes.models import CodeChangeAttempt, CodeChangeRequest
from ai_code_change_agent.domain.changes.multirepo import parse_story
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.orchestration.intake import ResumeHandler
from ai_code_change_agent.persistence.dynamodb import PersistenceError, Table, keys
from ai_code_change_agent.persistence.dynamodb.change_store import request_item
from ai_code_change_agent.persistence.dynamodb.multirepo_store import DynamoMultiRepoStore

pytestmark = [pytest.mark.floci, pytest.mark.integration]
T0 = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


async def seed_request(table: Table) -> uuid.UUID:
    request = CodeChangeRequest(uuid.uuid4(), str(uuid.uuid4().int)[:18], "SHOP-9", "SHOP")
    await table.put(request_item(request, created_at=T0))
    return request.id


def child(parent_id: uuid.UUID, position: int, key: str, *depends_on: str) -> Child:
    return Child(
        id=uuid.uuid4(),
        parent_id=parent_id,
        position=position,
        repository_id=uuid.uuid4(),
        profile_version_id=uuid.uuid4(),
        repository_key=key,
        reference=f"{key}-ref",
        role="api" if position == 0 else None,
        responsibility=None,
        depends_on=depends_on,
        status=ChildStatus.WAITING,
    )


async def new_parent(
    store: DynamoMultiRepoStore, table: Table, status: ParentStatus, n_children: int = 0
) -> tuple[Parent, list[Child]]:
    request_id = await seed_request(table)
    parent = Parent(uuid.uuid4(), request_id, status, {"repositories": [], "n": 1.5})
    kids = [
        child(parent.id, i, f"repo-{i}", *([f"repo-{i - 1}"] if i else []))
        for i in range(n_children)
    ]
    return await store.create(parent, kids)


async def finished_parent(store: DynamoMultiRepoStore, table: Table) -> uuid.UUID:
    parent, _ = await new_parent(store, table, ParentStatus.COMPLETED)
    return parent.id


def count_calls(table: Table, operation: str) -> list[str]:
    seen: list[str] = []
    table._client.meta.events.register(
        f"before-call.dynamodb.{operation}", lambda **_: seen.append(operation)
    )
    return seen


async def all_items(table: Table) -> list[dict[str, object]]:
    response = await asyncio.to_thread(table._client.scan, TableName=table.name)
    return list(response["Items"])


# M1-M5 ------------------------------------------------------------------------------------------


async def test_create_writes_42_items_in_one_transaction_and_is_idempotent(
    app_table: Table,
) -> None:
    store = DynamoMultiRepoStore(app_table, FakeClock())
    transactions = count_calls(app_table, "TransactWriteItems")
    parent, children = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=20)

    assert len(transactions) == 1
    assert len(await all_items(app_table)) == 1 + 42  # the seeded request + parent tree
    assert [c.position for c in children] == list(range(20))
    assert children[3].depends_on == ("repo-2",) and children[0].role == "api"
    assert parent.declaration == {"repositories": [], "n": 1.5}
    assert parent.report_round == 0 and parent.jira_report is None

    again = Parent(uuid.uuid4(), parent.request_id, ParentStatus.BLOCKED, {"other": True})
    replay = await store.create(again, [child(again.id, 0, "x")])
    assert replay == (parent, children)  # the stored parent wins; nothing else written
    assert len(await all_items(app_table)) == 1 + 42
    assert await store.find_by_request(parent.request_id) == (parent, children)
    assert await DynamoMultiRepoStore(app_table).load(parent.id) == (parent, children)
    assert await store.child(children[5].id) == children[5]


async def test_unknown_ids_and_duplicate_children(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table)
    assert await store.find_by_request(uuid.uuid4()) is None
    with pytest.raises(LookupError, match="parent_not_found"):
        await store.load(uuid.uuid4())
    assert await store.child(uuid.uuid4()) is None
    assert await store.child_by_attempt(uuid.uuid4()) is None
    assert await store.update_child(uuid.uuid4(), frozenset({ChildStatus.WAITING})) is False
    assert await store.update_parent(uuid.uuid4(), frozenset({ParentStatus.RUNNING})) is False
    assert await store.claim_report(uuid.uuid4(), stale_seconds=300) is None
    await store.finish_report(uuid.uuid4(), ReportState.SUCCEEDED)  # no-op
    pid = uuid.uuid4()
    twins = [child(pid, 0, "a"), child(pid, 0, "b")]
    with pytest.raises(ValueError, match="distinct"):
        await store.create(Parent(pid, uuid.uuid4(), ParentStatus.RUNNING, {}), twins)


# M6-M7 ------------------------------------------------------------------------------------------


async def test_update_child_is_compare_and_set_on_the_status(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table)
    _, (c,) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=1)

    assert not await store.update_child(
        c.id, frozenset({ChildStatus.QUEUED}), status=ChildStatus.RUNNING
    )
    assert await store.update_child(c.id, frozenset({ChildStatus.WAITING}), status="QUEUED")
    assert not await store.update_child(
        c.id, frozenset({ChildStatus.WAITING}), status=ChildStatus.BLOCKED
    )
    assert not await store.update_child(c.id, frozenset(), status=ChildStatus.BLOCKED)
    assert await store.update_child(
        c.id,
        frozenset({ChildStatus.QUEUED}),
        status=ChildStatus.COMPLETED,
        pr_url="https://github.example/pr/1",
        branch="acca/x",
        contract_ref="art_c",
        contract_sha256="d" * 64,
    )
    stored = await store.child(c.id)
    assert stored is not None and stored.status is ChildStatus.COMPLETED
    assert (stored.pr_url, stored.branch, stored.contract_sha256) == (
        "https://github.example/pr/1",
        "acca/x",
        "d" * 64,
    )
    with pytest.raises(ValueError, match="unknown child fields"):
        await store.update_child(c.id, frozenset({ChildStatus.COMPLETED}), position=3)
    with pytest.raises(ValueError, match="runs"):
        await store.update_child(c.id, frozenset({ChildStatus.COMPLETED}), runs=-1)


async def test_concurrent_transitions_let_exactly_one_win(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table)
    _, (c,) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=1)
    won = await asyncio.gather(
        *(
            store.update_child(c.id, frozenset({ChildStatus.WAITING}), status=ChildStatus.QUEUED)
            for _ in range(8)
        )
    )
    assert won.count(True) == 1
    stored = await store.child(c.id)
    assert stored is not None and stored.status is ChildStatus.QUEUED


async def test_racing_writers_that_all_match_the_status_each_apply_once(app_table: Table) -> None:
    """A lost version race re-reads and re-decides, like waiting for the SQL row lock: with 4
    writers each can lose at most 3 times, within the store's 5 attempts."""
    store = DynamoMultiRepoStore(app_table)
    _, (c,) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=1)
    codes = [f"code_{i}" for i in range(4)]
    applied = await asyncio.gather(
        *(store.update_child(c.id, frozenset({ChildStatus.WAITING}), failure_code=code)
          for code in codes)
    )  # fmt: skip
    assert applied == [True] * 4
    row = await app_table.get(keys.child((await store.load(c.parent_id))[0].request_id, c.id))
    assert row is not None and row["row_version"] == 5 and row["failure_code"] in codes


async def test_child_by_attempt_follows_the_link_written_with_the_attempt(
    app_table: Table,
) -> None:
    store = DynamoMultiRepoStore(app_table)
    _, (c, other) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=2)
    first, second = uuid.uuid4(), uuid.uuid4()
    running = frozenset({ChildStatus.QUEUED, ChildStatus.RUNNING, ChildStatus.WAITING})

    assert await store.update_child(c.id, running, status=ChildStatus.RUNNING, attempt_id=first)
    linked = await store.child_by_attempt(first)  # right after linking: strongly consistent
    assert linked is not None and linked.id == c.id and linked.attempt_id == first

    # a retry clears the attempt: the attempt is no longer a child, and its link is gone
    assert await store.update_child(
        c.id, frozenset({ChildStatus.RUNNING}), status=ChildStatus.WAITING, runs=1,
        failure_code=None, attempt_id=None,
    )  # fmt: skip
    assert await store.child_by_attempt(first) is None
    assert await app_table.get(keys.child_attempt_link(first)) is None

    # the next run links a new attempt; replacing it drops the old link in the same transaction
    assert await store.update_child(c.id, running, status=ChildStatus.RUNNING, attempt_id=first)
    assert await store.update_child(c.id, running, attempt_id=second)
    assert await store.child_by_attempt(first) is None
    assert await app_table.get(keys.child_attempt_link(first)) is None
    relinked = await store.child_by_attempt(second)
    assert relinked is not None and relinked.id == c.id and relinked.runs == 1

    # an attempt already linked to a sibling is refused, and nothing changes
    with pytest.raises(PersistenceError, match=r"^attempt_linked_to_another_child$"):
        await store.update_child(other.id, running, attempt_id=second)
    stored = await store.child(other.id)
    assert stored is not None and stored.attempt_id is None


async def test_a_stale_link_is_not_trusted(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table)
    _, (c,) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=1)
    orphan = uuid.uuid4()
    await app_table.put(
        {
            **keys.child_attempt_link(orphan).as_dict(),
            "request_id": str(c.parent_id),
            "child_id": str(c.id),
        }
    )
    assert await store.child_by_attempt(orphan) is None  # wrong request: no child item
    stored = await store.child(c.id)
    assert stored is not None
    rid = (await store.load(c.parent_id))[0].request_id
    await app_table.put(
        {
            **keys.child_attempt_link(orphan).as_dict(),
            "request_id": str(rid),
            "child_id": str(c.id),
        }
    )
    assert await store.child_by_attempt(orphan) is None  # child.attempt_id differs


# M8-M11 -----------------------------------------------------------------------------------------


async def test_report_lifecycle_persists_and_is_claimed_once(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table, FakeClock())
    pid = await finished_parent(store, app_table)
    claim = await store.claim_report(pid, stale_seconds=300)
    assert claim is not None and claim.previous is ReportState.PENDING and claim.attempts == 1
    assert claim.round == 0
    assert await store.claim_report(pid, stale_seconds=300) is None  # in flight: nobody else
    await store.finish_report(pid, ReportState.RETRYABLE_FAILURE, error="http_503")
    parent, _ = await store.load(pid)
    assert parent.jira_report == "RETRYABLE_FAILURE" and parent.jira_report_error == "http_503"
    retry = await store.claim_report(pid, stale_seconds=300)
    assert retry is not None and retry.previous is ReportState.RETRYABLE_FAILURE
    assert retry.attempts == 2
    await store.finish_report(pid, ReportState.SUCCEEDED, comment_id="10009")
    parent, _ = await DynamoMultiRepoStore(app_table).load(pid)  # "after a restart"
    assert parent.jira_report == "SUCCEEDED" and parent.jira_comment_id == "10009"
    assert parent.jira_report_attempts == 2 and parent.jira_report_error is None
    assert parent.status is ParentStatus.COMPLETED  # execution status is separate
    assert await store.claim_report(pid, stale_seconds=300) is None  # never posted twice
    await store.finish_report(pid, ReportState.PERMANENT_FAILURE, error="late")  # not in flight
    assert (await store.load(pid))[0].jira_report == "SUCCEEDED"


async def test_concurrent_claims_have_one_winner(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table, FakeClock())
    pid = await finished_parent(store, app_table)
    claims = await asyncio.gather(*(store.claim_report(pid, 300) for _ in range(8)))
    assert sum(claim is not None for claim in claims) == 1


async def test_a_crashed_in_progress_claim_is_recovered_only_when_stale(app_table: Table) -> None:
    clock = FakeClock()
    store = DynamoMultiRepoStore(app_table, clock)
    pid = await finished_parent(store, app_table)
    assert await store.claim_report(pid, stale_seconds=300) is not None  # worker dies here
    clock.advance(299)
    assert await store.claim_report(pid, stale_seconds=300) is None
    clock.advance(1)
    assert await store.claim_report(pid, stale_seconds=300) is None  # exactly 300 s: not older
    clock.advance(0.001)
    recovered = await store.claim_report(pid, stale_seconds=300)
    assert recovered is not None and recovered.previous is ReportState.IN_PROGRESS
    assert recovered.attempts == 2
    assert await store.claim_report(pid, stale_seconds=300) is None  # the new claim is fresh


async def test_a_new_report_round_resets_the_report_but_keeps_history_in_jira(
    app_table: Table,
) -> None:
    clock = FakeClock()
    store = DynamoMultiRepoStore(app_table, clock)
    parent, _ = await new_parent(store, app_table, ParentStatus.RUNNING)
    assert not await store.update_parent(
        parent.id, frozenset({ParentStatus.COMPLETED}), status=ParentStatus.FAILED
    )
    assert await store.update_parent(
        parent.id, frozenset({ParentStatus.RUNNING}), status=ParentStatus.COMPLETED, finished=True
    )
    done, _ = await store.load(parent.id)
    assert done.status is ParentStatus.COMPLETED and done.completed_at == T0
    await store.claim_report(parent.id, stale_seconds=300)
    await store.finish_report(parent.id, ReportState.SUCCEEDED, comment_id="10001")
    assert await store.update_parent(
        parent.id, frozenset({ParentStatus.COMPLETED}), status=ParentStatus.RUNNING, reopen=True
    )
    reopened, _ = await store.load(parent.id)
    assert reopened.report_round == 1 and reopened.jira_report is None
    assert reopened.jira_comment_id is None and reopened.jira_report_attempts == 0
    assert reopened.completed_at is None and reopened.status is ParentStatus.RUNNING
    claim = await store.claim_report(parent.id, stale_seconds=300)
    assert claim is not None and claim.round == 1 and claim.previous is ReportState.PENDING
    with pytest.raises(ValueError, match="unknown parent fields"):
        await store.update_parent(parent.id, frozenset({ParentStatus.RUNNING}), jira_report="X")


async def test_request_status_projection_bumps_the_version(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table, FakeClock(T0 + timedelta(minutes=1)))
    rid = await seed_request(app_table)
    await store.set_request_status(rid, "PENDING")
    item = await app_table.get(keys.request(rid))
    assert item is not None and item["status"] == "PENDING" and item["row_version"] == 2
    assert item["updated_at"] == "2026-11-27T09:01:00.000000Z"
    missing = uuid.uuid4()
    await store.set_request_status(missing, "PENDING")  # no-op, never creates a request
    assert await app_table.get(keys.request(missing)) is None


async def test_corrupt_items_raise_without_values(app_table: Table) -> None:
    store = DynamoMultiRepoStore(app_table)
    parent, (c,) = await new_parent(store, app_table, ParentStatus.RUNNING, n_children=1)
    item = await app_table.get(keys.parent(parent.request_id))
    assert item is not None
    await app_table.put({**item, "declaration_json": "[1]"})
    with pytest.raises(PersistenceError, match=r"^corrupt_item:declaration_json$"):
        await store.load(parent.id)
    await app_table.put({**item, "jira_report": "POSTED"})
    with pytest.raises(PersistenceError, match=r"^corrupt_item:jira_report$"):
        await store.load(parent.id)
    await app_table.put(item)
    row = await app_table.get(keys.child(parent.request_id, c.id))
    assert row is not None
    await app_table.put({**row, "depends_on_json": '{"a": 1}'})
    with pytest.raises(PersistenceError, match=r"^corrupt_item:depends_on_json$"):
        await store.child(c.id)


# S2117: children paused at a manual gate finish through the RESUME job ------------------------


async def test_children_resumed_after_their_gates_finish_the_parent_once(
    app_table: Table, tmp_path: Path
) -> None:
    from tests.unit.multirepo.test_multirepo import (
        TWO,
        Queue,
        Reporter,
        Resolver,
        ResumeChanges,
        artifact_service,
        durable,
        expired,
        resume_job,
    )

    store = DynamoMultiRepoStore(app_table)
    changes, reporter = ResumeChanges(), Reporter()
    coordinator = Coordinator(
        store=store, changes=changes, queue=Queue(),  # type: ignore[arg-type]
        artifacts=artifact_service(tmp_path / "artifacts"), resolver=Resolver(),
        reporter=durable(reporter), max_repositories=5, max_job_attempts=3,
    )  # fmt: skip
    request_id = await seed_request(app_table)
    parent = await coordinator.start(request_id, "SHOP-9", "SHOP", parse_story(TWO))
    _, children = await store.load(parent.id)
    attempts: dict[str, CodeChangeAttempt] = {}
    for c in children:  # each CHILD job ran up to its plan gate and SUCCEEDED
        attempt = CodeChangeAttempt(
            uuid.uuid4(), request_id, 1, c.repository_id, c.profile_version_id,
            status=AttemptStatus.AWAITING_PLAN_APPROVAL,
        )  # fmt: skip
        changes.attempts[attempt.id] = attempt
        assert await store.update_child(
            c.id, frozenset({ChildStatus.QUEUED}), status=ChildStatus.RUNNING, attempt_id=attempt.id
        )
        attempts[c.repository_key] = attempt

    async def approved(attempt_id: uuid.UUID) -> None:
        changes.attempts[attempt_id] = replace(
            changes.attempts[attempt_id], status=AttemptStatus.COMPLETED
        )

    def handler(resume: Any) -> ResumeHandler:
        return ResumeHandler(resume, changes=changes, coordinator=coordinator)  # type: ignore[arg-type]

    async def state() -> tuple[ParentStatus, dict[str, Child]]:
        loaded, kids = await store.load(parent.id)
        return loaded.status, {c.repository_key: c for c in kids}

    await handler(approved)(resume_job(attempts["backend-api"]))
    status, kids = await state()
    assert kids["backend-api"].status is ChildStatus.COMPLETED
    assert kids["web-app"].status is ChildStatus.RUNNING and status is ParentStatus.RUNNING
    job = resume_job(attempts["web-app"])
    for _ in range(2):  # the expired child, then its redelivered RESUME job
        with pytest.raises(JobFailedError, match="checkpoint_expired"):
            await handler(expired)(job)
    status, kids = await state()
    assert kids["web-app"].status is ChildStatus.FAILED
    assert kids["web-app"].failure_code == "checkpoint_expired"
    assert status is ParentStatus.PARTIALLY_COMPLETED  # not RUNNING once every child is terminal
    await handler(approved)(resume_job(attempts["backend-api"]))  # a duplicate notification
    loaded, _ = await store.load(parent.id)
    assert loaded.jira_report == ReportState.SUCCEEDED.value
    assert len(reporter.texts) == 1 and "Reason: checkpoint_expired" in reporter.texts[0][1]
