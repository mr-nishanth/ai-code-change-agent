"""Studio endpoints without SQL (DYNAMODB-ACCESS-PATTERNS.md L3, L4, S1-S4) on the local Floci emulator.

Each test gets its own application table, checkpoint table and bucket (unique `s2116-<uuid>` names,
deleted afterwards). The studio reads them through `studio.stores` built from test Settings (explicit
loopback endpoint, static `test`/`test` credentials); `AWS_PROFILE` and `AWS_ENDPOINT_URL` are set
to unusable values by `app_table` to prove they are ignored. Seeding goes through the application's
own ports; agent checkpoints are written by a small stand-in graph with the real `DynamoDBSaver`."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, TypedDict

import httpx
import pytest
import studio.live
import studio.stores
import studio.workspaces
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from studio.api import app
from studio.stores import StudioStores, build_stores

from ai_code_change_agent.agents.coding.configuration import AgentPhase
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.domain.changes.errors import ChangeNotFoundError
from ai_code_change_agent.domain.changes.models import CodeChangeRequest
from ai_code_change_agent.domain.changes.states import AttemptStatus as S
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.change_store import (
    AttemptPointers,
    DynamoChangeUnitOfWork,
    request_item,
)
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore
from tests.support.dynamo import create_table, local_clients
from tests.support.floci import unique_name
from tests.support.registry_fixtures import new_repo

pytestmark = pytest.mark.floci
PR = "https://github.com/acme/shop-service/pull/7"
T0 = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


class StepClock:
    """Each call is one second later, so creation order is the listing order."""

    def __init__(self) -> None:
        self._at = T0

    def now(self) -> datetime:
        self._at += timedelta(seconds=1)
        return self._at


@dataclass
class Env:
    table: Table
    settings: Settings
    stores: StudioStores
    changes: CodeChangeService  # writes, as the worker would


@pytest.fixture
def env(app_table: Table, floci: str) -> Iterator[Env]:
    admin = local_clients(floci)
    checkpoints = create_table(admin.dynamodb, "acca-checkpoints")
    bucket = unique_name("s2116")
    admin.s3.create_bucket(Bucket=bucket)
    settings = Settings(
        _env_file=None,
        app_env="test",
        ACCA_AWS_ENDPOINT_URL=floci,
        ACCA_DYNAMODB_TABLE_NAME=app_table.name,
        ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME=checkpoints,
        ACCA_S3_BUCKET_NAME=bucket,
    )
    clock = StepClock()
    pointers = AttemptPointers()
    writer = CodeChangeService(lambda: DynamoChangeUnitOfWork(app_table, clock, pointers=pointers))
    try:
        yield Env(app_table, settings, build_stores(settings), writer)
    finally:
        for page in admin.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for entry in page.get("Contents", []):
                admin.s3.delete_object(Bucket=bucket, Key=entry["Key"])
        admin.s3.delete_bucket(Bucket=bucket)
        admin.dynamodb.delete_table(TableName=checkpoints)


@pytest.fixture
async def client(env: Env, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(studio.workspaces, "_settings", env.settings)
    monkeypatch.setattr(studio.stores, "_stores", None)  # built from the test Settings
    monkeypatch.setattr(studio.live, "_coding_agent", lambda: StandInAgent())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://studio") as c:
        yield c


async def seed_attempt(env: Env, issue: str, repository_key: str) -> uuid.UUID:
    async with DynamoRegistryUnitOfWork(env.table) as uow:
        repo = await uow.store.add_repository(new_repo(repository_key), created_by="cli:t")
        await uow.commit()
    request = CodeChangeRequest(
        uuid.uuid4(), f"id-{issue}", issue, issue.split("-")[0], repo.id, uuid.uuid4()
    )
    await env.table.put(request_item(request, created_at=T0))
    return (await env.changes.start_attempt(request.id)).id


def call(attempt_id: uuid.UUID, stage: str, usage: TokenUsage | None, cost: str | None) -> Any:
    return LlmCallRecord(
        call_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        attempt_id=attempt_id,
        stage=stage,
        provider="anthropic",
        model="claude-haiku-5-5",
        succeeded=True,
        usage=usage,
        cost_usd=Decimal(cost) if cost is not None else None,
        pricing_version="test",
    )


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    todos: list[dict[str, str]]
    structured_response: dict[str, str]


class StandInAgent:
    """Stands in for the CodingAgent graph: the same state keys, no model, no sandbox. The
    implementation graph pauses before its node, as a run still in progress would look."""

    def build(self, phase: AgentPhase, sandbox: Any, *, checkpointer: Any = None) -> Any:
        def agent(state: AgentState) -> AgentState:
            return {
                "messages": [AIMessage(f"{phase.value} done")],
                "todos": [{"content": "a", "status": "completed"}],
                "structured_response": {"summary": f"{phase.value} result"},
            }

        graph = StateGraph(AgentState)
        graph.add_node("agent", agent)
        graph.add_edge(START, "agent")
        graph.add_edge("agent", END)
        interrupt = ["agent"] if phase is AgentPhase.IMPLEMENTATION else None
        return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt)


async def run_agent(env: Env, phase: AgentPhase, thread_id: str) -> None:
    graph = StandInAgent().build(phase, None, checkpointer=env.stores.checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke({"messages": [HumanMessage(f"start {phase.value}")]}, config)


# Ports ------------------------------------------------------------------------------------------


async def test_recent_attempts_come_newest_first_from_gsi2(env: Env) -> None:
    first = await seed_attempt(env, "SHOP-1", "shop-service")
    second = await seed_attempt(env, "SHOP-2", "billing-service")
    await env.changes.transition(first, S.PREPARING)
    listed = await env.stores.changes.recent_attempts(10)
    assert [x.attempt.id for x in listed] == [second, first]
    assert listed[1].attempt.status is S.PREPARING and listed[1].updated_at > listed[1].created_at
    assert [x.attempt.id for x in await env.stores.changes.recent_attempts(1)] == [second]
    assert (await env.stores.changes.attempt_listing(first)).attempt.number == 1
    with pytest.raises(ChangeNotFoundError):
        await env.stores.changes.attempt_listing(uuid.uuid4())


async def test_agent_threads_are_indexed_once_in_run_order(env: Env) -> None:
    aid = await seed_attempt(env, "SHOP-1", "shop-service")
    planning = f"attempt:{aid}:planning:0000000f"
    implementation = f"attempt:{aid}:implementation:0:00000001"
    await env.changes.record_agent_thread(aid, planning)
    await env.changes.record_agent_thread(aid, implementation)
    await env.changes.record_agent_thread(aid, planning)  # a repeat changes nothing
    assert await env.stores.changes.agent_threads(aid) == [planning, implementation]
    item = await env.table.get(keys.agent_thread(aid, planning))
    assert item is not None and item["PK"] == f"ATTEMPT#{aid}" and item["entity"] == "agent_thread"
    with pytest.raises(ValueError, match="does not belong"):
        await env.changes.record_agent_thread(aid, f"attempt:{uuid.uuid4()}:planning:00000000")
    other = uuid.uuid4()
    with pytest.raises(ChangeNotFoundError):
        await env.changes.record_agent_thread(other, f"attempt:{other}:planning:00000000")
    assert await env.stores.changes.agent_threads(uuid.uuid4()) == []


# Endpoints --------------------------------------------------------------------------------------


async def test_attempt_history_joins_request_repository_costs_and_pr_link(
    env: Env, client: httpx.AsyncClient
) -> None:
    shipped = await seed_attempt(env, "SHOP-1", "shop-service")
    fresh = await seed_attempt(env, "SHOP-2", "billing-service")
    ledger = DynamoUsageStore(env.table)
    await ledger.add(call(shipped, "planning", TokenUsage(1_000, 200, 300, 0, 500), "0.004"))
    await ledger.add(call(shipped, "implementation", TokenUsage(2_000, 100), "0.006"))
    await ledger.add(call(shipped, "implementation", None, None))  # usage not reported
    req = (await env.changes.get_attempt(shipped)).request_id
    scope = ArtifactScope(req, shipped)
    await env.stores.artifacts.put(scope, ArtifactKind.CHANGESET, b"{}")
    await env.stores.artifacts.put(
        scope, ArtifactKind.DELIVERY_RESULT, json.dumps({"pr_url": PR}).encode()
    )

    res = await client.get("/studio/attempts", params={"limit": 10})
    assert res.status_code == 200
    body = res.json()
    assert "error" not in body
    newest, older = body["attempts"]
    assert (newest["attempt_id"], newest["issue"], newest["repository"]) == (
        str(fresh),
        "SHOP-2",
        "billing-service",
    )
    assert newest["cost_usd"] is None and newest["pr_url"] is None and newest["calls"] == 0
    assert (older["issue"], older["repository"], older["status"]) == (
        "SHOP-1",
        "shop-service",
        "PENDING",
    )
    assert (older["calls"], older["unknown_usage_calls"], older["cost_usd"]) == (3, 1, "0.010000")
    assert older["stage_costs"] == {"planning": "0.004000", "implementation": "0.006000"}
    assert (older["tokens_in"], older["tokens_out"]) == (1_800 + 2_000, 300)
    assert older["models"] == ["claude-haiku-5-5"] and older["pr_url"] == PR
    assert older["created_at"].startswith("2026-11-27T09:00")
    limited = (await client.get("/studio/attempts", params={"limit": 1})).json()["attempts"]
    assert [a["attempt_id"] for a in limited] == [str(fresh)]


async def test_live_view_reads_indexed_threads_from_the_dynamodb_checkpointer(
    env: Env, client: httpx.AsyncClient
) -> None:
    aid = await seed_attempt(env, "SHOP-1", "shop-service")
    planning = f"attempt:{aid}:planning:0000000a"
    implementation = f"attempt:{aid}:implementation:1:0000000b"
    crashed = f"attempt:{aid}:implementation:0:0000000c"  # indexed, never checkpointed
    for thread_id in (planning, crashed, implementation):
        await env.changes.record_agent_thread(aid, thread_id)
    await run_agent(env, AgentPhase.PLANNING, planning)
    await run_agent(env, AgentPhase.IMPLEMENTATION, implementation)
    await env.table.put(  # an index entry that is not an agent run is ignored
        {
            **keys.agent_thread(aid, f"attempt:{aid}").as_dict(),
            "entity": "agent_thread",
            "attempt_id": str(aid),
            "thread_id": f"attempt:{aid}",
            "started_at": "0000",
        }
    )

    res = await client.get(f"/studio/attempts/{aid}/live")
    assert res.status_code == 200
    body = res.json()
    assert body["attempt"]["issue"] == "SHOP-1" and body["attempt"]["repository"] == "shop-service"
    assert body["terminal"] is False
    plan, empty, impl = body["phases"]
    assert (plan["thread_id"], plan["stage"], plan["running"]) == (planning, "planning", False)
    assert [m["data"]["content"] for m in plan["messages"]] == ["start planning", "planning done"]
    assert plan["result"] == {"summary": "planning result"} and plan["todos"][0]["status"]
    assert (empty["thread_id"], empty["messages"], empty["running"]) == (crashed, [], False)
    assert (impl["stage"], impl["repairs"], impl["running"]) == ("correction", 1, True)
    assert [m["data"]["content"] for m in impl["messages"]] == ["start implementation"]

    await env.changes.transition(aid, S.CANCELLED)
    assert (await client.get(f"/studio/attempts/{aid}/live")).json()["terminal"] is True
    missing = await client.get(f"/studio/attempts/{uuid.uuid4()}/live")
    assert missing.status_code == 404


async def test_studio_stores_ignore_ambient_aws_settings(env: Env) -> None:
    """`app_table` set AWS_PROFILE and AWS_ENDPOINT_URL to unusable values: the studio's own
    clients still reach Floci (a call through them would fail otherwise)."""
    assert await env.stores.changes.recent_attempts(5) == []
    assert AwsClients(env.settings).endpoint_url == env.settings.aws_endpoint_url
