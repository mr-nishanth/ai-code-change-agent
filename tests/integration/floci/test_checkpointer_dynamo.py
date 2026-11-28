"""The DynamoDB LangGraph checkpointer (`dynamodb_checkpointer`, DM 9.4) on the local Floci emulator.

Every test gets its own checkpoint table (schema `acca-checkpoints`) and bucket, both under unique
`s2116-<uuid>` names and deleted afterwards. Clients come from `AwsClients` with test Settings
(explicit loopback endpoint, static `test`/`test` credentials); `AWS_PROFILE` and `AWS_ENDPOINT_URL`
are set to unusable values to prove they are ignored."""

import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, TypedDict, cast

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.conformance import checkpointer_test, validate
from langgraph.checkpoint.conformance.capabilities import BASE_CAPABILITIES
from langgraph.checkpoint.conformance.report import ProgressCallbacks
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph_checkpoint_aws.checkpoint.dynamodb.storage_strategy import (  # type: ignore[import-untyped]
    S3_OFFLOAD_THRESHOLD,
)

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.orchestration.langgraph.checkpointer import (
    CHECKPOINT_S3_PREFIX,
    dynamodb_checkpointer,
)
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.ports import GateState
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from tests.support.dynamo import create_table
from tests.support.floci import unique_name
from tests.unit.orchestration.test_workflow import ScriptedSteps, happy

DYNAMODB_ITEM_LIMIT = 400 * 1024


@dataclass(frozen=True)
class Env:
    settings: Settings
    admin: AwsClients  # for assertions and cleanup; the saver builds its own clients

    @property
    def table(self) -> str:
        return self.settings.dynamodb_checkpoint_table_name

    @property
    def bucket(self) -> str:
        return self.settings.s3_bucket_name

    def saver(self) -> BaseCheckpointSaver[str]:
        """A fresh saver on fresh clients, as a new worker process would build it."""
        return dynamodb_checkpointer(AwsClients(self.settings), self.settings)

    def table_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"TableName": self.table, "ConsistentRead": True}
        while True:
            page = self.admin.dynamodb.scan(**kwargs)
            items.extend(page.get("Items", []))
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def s3_keys(self) -> list[str]:
        page = self.admin.s3.list_objects_v2(Bucket=self.bucket)
        return [str(o["Key"]) for o in page.get("Contents", [])]


@pytest.fixture
def env(floci: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    bootstrap = AwsClients(Settings(_env_file=None, app_env="test", ACCA_AWS_ENDPOINT_URL=floci))
    table = create_table(bootstrap.dynamodb, "acca-checkpoints")
    bucket = unique_name("s2116")
    bootstrap.s3.create_bucket(Bucket=bucket)
    settings = Settings(
        _env_file=None,
        app_env="test",
        ACCA_AWS_ENDPOINT_URL=floci,
        ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME=table,
        ACCA_S3_BUCKET_NAME=bucket,
    )
    try:
        yield Env(settings, AwsClients(settings))
    finally:
        for key in Env(settings, bootstrap).s3_keys():
            bootstrap.s3.delete_object(Bucket=bucket, Key=key)
        bootstrap.s3.delete_bucket(Bucket=bucket)
        bootstrap.dynamodb.delete_table(TableName=table)


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def test_the_official_conformance_suite_passes_in_full(env: Env) -> None:
    @checkpointer_test(name=f"dynamodb_checkpointer-{uuid.uuid4().hex[:8]}")
    async def factory() -> AsyncIterator[BaseCheckpointSaver[str]]:
        yield env.saver()

    report = await validate(factory, progress=ProgressCallbacks.quiet())
    report.print_report()
    assert len(BASE_CAPABILITIES) == 5
    assert report.passed_all_base(), report.to_dict()
    assert report.conformance_level() == "FULL", report.to_dict()


async def test_the_runner_starts_on_one_saver_and_resumes_on_another(env: Env) -> None:
    """The real WorkflowRunner with scripted steps: worker A stops at the plan gate; worker B (new
    clients, new saver) sees the gate, resumes after approval and finishes; deleting the thread
    leaves nothing behind."""
    steps = ScriptedSteps(happy())
    steps.gates["PLAN"] = GateState.PENDING
    attempt = uuid.uuid4()
    await WorkflowRunner(build_graph(steps, env.saver())).start(attempt)
    assert steps.calls[-1] == "gate:PLAN"

    worker_b = WorkflowRunner(build_graph(steps, env.saver()))
    assert await worker_b.waiting_for(attempt) == "PLAN"
    await worker_b.resume(attempt)  # woken without a decision: stays paused
    assert await worker_b.waiting_for(attempt) == "PLAN"
    steps.gates["PLAN"] = GateState.APPROVED
    await worker_b.resume(attempt)
    assert steps.calls.count("prepare") == 1 and steps.calls[-1] == "cleanup"
    assert await WorkflowRunner(build_graph(steps, env.saver())).waiting_for(attempt) is None
    await WorkflowRunner(build_graph(steps, env.saver())).start(attempt)  # never runs twice
    assert steps.calls.count("prepare") == 1

    assert env.table_items()
    await env.saver().adelete_thread(f"attempt:{attempt}")
    assert env.table_items() == []


class BlobState(TypedDict, total=False):
    blob: str
    approved: bool


def _blob_graph(saver: BaseCheckpointSaver[str], blob: str) -> Any:
    def produce(state: BlobState) -> BlobState:
        return {"blob": blob}

    def gate(state: BlobState) -> BlobState:
        return {"approved": bool(interrupt({"awaiting": "PLAN"}))}

    graph = StateGraph(BlobState)
    graph.add_node("produce", produce)
    graph.add_node("gate", gate)
    graph.add_edge(START, "produce")
    graph.add_edge("produce", "gate")
    graph.add_edge("gate", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.parametrize("size", [600_000, 1_300_000], ids=["600KB", "1.3MB"])
async def test_large_states_are_offloaded_round_tripped_and_deleted(env: Env, size: int) -> None:
    blob = secrets.token_hex(size // 2)  # incompressible, exactly `size` characters
    thread = f"attempt:{uuid.uuid4()}"
    await _blob_graph(env.saver(), blob).ainvoke({}, _config(thread))
    keys = env.s3_keys()
    assert keys and all(k.startswith(CHECKPOINT_S3_PREFIX) for k in keys)

    resumed = _blob_graph(env.saver(), "")  # another worker; the node must not re-run
    state = await resumed.aget_state(_config(thread))
    assert [i.value for t in state.tasks for i in t.interrupts] == [{"awaiting": "PLAN"}]
    result = await resumed.ainvoke(Command(resume=True), _config(thread))
    assert result["approved"] is True and result["blob"] == blob

    await env.saver().adelete_thread(thread)
    assert env.table_items() == []
    assert env.s3_keys() == []


def _payload(env: Env, chunk_key: str) -> bytes | None:
    item = env.admin.dynamodb.get_item(
        TableName=env.table, Key={"PK": {"S": chunk_key}, "SK": {"S": "CHUNK"}}, ConsistentRead=True
    ).get("Item")
    return None if item is None else bytes(item["payload"]["B"])


def test_the_offload_threshold_is_358400_bytes_per_payload(env: Env) -> None:
    """At exactly the threshold a payload stays in its DynamoDB chunk item; one byte over goes to
    S3 under the checkpoint prefix (DM 9.2)."""
    assert S3_OFFLOAD_THRESHOLD == 358_400
    storage = cast(Any, env.saver()).storage
    at, over = b"a" * 358_400, b"b" * 358_401

    assert storage.store_data("CHUNK_at", "at", at) == "DYNAMODB"
    assert _payload(env, "CHUNK_at") == at
    assert storage.retrieve_data("CHUNK_at", "DYNAMODB") == at
    assert env.s3_keys() == []

    assert storage.store_data("CHUNK_over", "over", over) == "S3"
    assert _payload(env, "CHUNK_over") is None
    assert env.s3_keys() == [f"{CHECKPOINT_S3_PREFIX}over"]
    assert storage.retrieve_data("over", "S3") == over


def test_floci_enforces_the_dynamodb_item_size_limit(env: Env) -> None:
    """Proves the offload is needed on Floci too: a 410 KB item is refused, not silently stored."""
    with pytest.raises(ClientError) as raised:
        env.admin.dynamodb.put_item(
            TableName=env.table,
            Item={
                "PK": {"S": "CHUNK_big"},
                "SK": {"S": "CHUNK"},
                "payload": {"B": b"x" * 410 * 1024},
            },
        )
    assert raised.value.response["Error"]["Code"] == "ValidationException"
    assert DYNAMODB_ITEM_LIMIT < 410 * 1024
    assert env.table_items() == []


async def test_checkpoints_expire_after_the_configured_retention(env: Env) -> None:
    """ACCA_CHECKPOINT_TTL_DAYS (default 30) is the items' `ttl` and the S3 lifecycle rule."""
    assert env.settings.checkpoint_ttl_days == 30
    started = time.time()
    await _blob_graph(env.saver(), "small").ainvoke({}, _config(f"attempt:{uuid.uuid4()}"))
    expiries = [int(i["ttl"]["N"]) for i in env.table_items() if "ttl" in i]
    assert expiries
    assert all(started + 30 * 86_400 - 5 <= t <= time.time() + 30 * 86_400 + 5 for t in expiries)
    rules = env.admin.s3.get_bucket_lifecycle_configuration(Bucket=env.bucket)["Rules"]
    assert [r["Expiration"]["Days"] for r in rules] == [30]
