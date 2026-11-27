"""S2116 Task 18: the whole pipeline, locally and deterministically, on Floci and Docker.

Signed synthetic Jira webhook -> FastAPI app (in-process ASGI) -> DynamoDB (delivery, request, job +
outbox) -> dispatcher relay -> SQS -> worker claim -> intake with fake Jira REST -> resolver on a
registry seeded in DynamoDB -> workflow with a scripted tool-calling model (planning in a read-only
Docker sandbox) -> plan gate interrupt -> CLI approve -> resume job -> implementation in a writable
Docker sandbox on a real (local, bare) Git repository -> independent validation (pytest in the
execution image) -> final gate -> CLI approve -> delivery to the real GitHub publisher over FakeGitHub.

Reliability on the way, in the same run: a duplicate webhook is `duplicate`; a duplicate SQS message
is a no-op; the worker is SIGKILLed in the middle of the resume job (inside the implementation run,
sandbox running) and a restarted worker completes that same job once; the final resume message is
deleted out of band and the sweep re-sends it; the planning run's checkpoint is over 400 KB and is
offloaded to S3 and read back intact by a fresh saver. A second test sends poison messages to the DLQ.

Nothing reaches the network: Jira and GitHub are mock transports, the model is scripted, AWS is Floci
on a loopback endpoint with dummy credentials. DynamoDB request units are measured
(`ReturnConsumedCapacity=TOTAL`, `tests/e2e/capacity.py`) and printed (`-rP`)."""

import asyncio
import hashlib
import io
import json
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_code_change_agent.agents.coding.agent import CodingAgent
from ai_code_change_agent.agents.coding.configuration import AgentPhase
from ai_code_change_agent.application.jobs.queue import JobStatus
from ai_code_change_agent.cli.main import main as cli_main
from ai_code_change_agent.composition import (
    WorkerApp,
    app_table,
    build_change_service,
    build_registry_service,
    worker_app,
)
from ai_code_change_agent.core.config import Settings, get_settings
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.execution.docker.sandbox import DockerSandbox, SandboxLimits
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.main import create_app
from ai_code_change_agent.orchestration.langgraph.checkpointer import (
    CHECKPOINT_S3_PREFIX,
    dynamodb_checkpointer,
)
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore
from tests.e2e import capacity
from tests.e2e.conftest import Resources, docker
from tests.e2e.world import (
    BIG,
    CHANGED,
    ISSUE_KEY,
    LEDGER_PROVIDER,
    PROFILE,
    PROJECT,
    REPO_FILES,
    REPO_ID,
    World,
    build_world,
    dump,
    implementation_script,
    install,
    make_settings,
    planning_script,
    settings_document,
)
from tests.support.github_repos import commit
from tests.support.jira import body_bytes, headers
from tests.support.models import ScriptedChatModel
from tests.support.registry_fixtures import new_repo

pytestmark = [pytest.mark.floci, pytest.mark.docker, pytest.mark.integration]
REPO = Path(__file__).resolve().parents[2]
WEBHOOK = "/webhooks/jira"
OFFLOAD_THRESHOLD = 358_400  # DynamoDBSaver's per-payload limit (DM 9.2)


# ---- helpers ------------------------------------------------------------------------------------
async def until[T](fn: Callable[[], Awaitable[T]], what: str, limit: float = 120) -> T:
    deadline = time.monotonic() + limit
    while True:
        value = await fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {limit:.0f}s waiting for {what}")
        await asyncio.sleep(0.25)


def scan(aws: AwsClients, table: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"TableName": table, "ConsistentRead": True}
    while True:
        page = aws.dynamodb.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def depth(aws: AwsClients, url: str) -> int:
    names = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
             "ApproximateNumberOfMessagesDelayed"]  # fmt: skip
    attrs = aws.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=names)["Attributes"]
    return sum(int(attrs.get(n, 0)) for n in names)


@dataclass
class Rig:
    res: Resources
    settings: Settings
    document: dict[str, Any]
    bare: Path
    world: World
    inspect: AwsClients  # built before the capacity counter: test reads are never counted
    tally: capacity.Tally
    attempts: list[uuid.UUID] = field(default_factory=list)

    def jobs(self) -> list[dict[str, Any]]:
        out = []
        for item in scan(self.inspect, self.res.table):
            if item["PK"]["S"].startswith("JOB#") and item["SK"]["S"] == "JOB":
                out.append(item)
        return out

    def job(self, kind: str, nth: int = 0) -> dict[str, Any]:
        found = sorted(
            (j for j in self.jobs() if j["kind"]["S"] == kind), key=lambda j: j["created_at"]["S"]
        )
        return found[nth]

    def job_by_id(self, job_id: str) -> dict[str, Any]:
        (item,) = [j for j in self.jobs() if j["id"]["S"] == job_id]
        return item

    def queues_empty(self) -> bool:
        return (
            depth(self.inspect, self.res.queue_url) == 0
            and depth(self.inspect, self.res.dlq_url) == 0
        )


@pytest.fixture
def rig(resources: Resources, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    document = settings_document(
        endpoint=resources.endpoint,
        table=resources.table,
        checkpoint_table=resources.checkpoint_table,
        queue_name=resources.queue_name,
        bucket=resources.bucket,
        root=resources.root,
        key_path=resources.key_path,
        image=resources.image,
    )
    settings = make_settings(document)
    bare = resources.root / "remote.git"
    commit(bare, REPO_FILES)
    world = build_world(
        settings, bare, ScriptedChatModel(script=planning_script() + implementation_script())
    )
    install(world, monkeypatch.setattr)
    inspect = AwsClients(settings)
    tally = capacity.Tally(
        roles={resources.table: "app", resources.checkpoint_table: "checkpoints"}
    )
    capacity.install(monkeypatch, tally)
    # the operator CLI reads its Settings from the environment, like a shell would
    for name, value in {
        "APP_ENV": "test",
        "ACCA_AWS_ENDPOINT_URL": resources.endpoint,
        "ACCA_DYNAMODB_TABLE_NAME": resources.table,
        "ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME": resources.checkpoint_table,
        "ACCA_SQS_JOB_QUEUE_NAME": resources.queue_name,
        "ACCA_S3_BUCKET_NAME": resources.bucket,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    made = Rig(resources, settings, document, bare, world, inspect, tally)
    try:
        yield made
    finally:  # never leave a sandbox behind: it would make every later e2e run a skip
        for attempt_id in made.attempts:
            for name in sandboxes_of(attempt_id):
                docker("rm", "-f", name)


@pytest.fixture(autouse=True)
def _fresh_cli_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def seed_registry(rig: Rig) -> None:
    svc = build_registry_service(rig.settings, rig.inspect)
    actor = "cli:e2e"
    await svc.register_repository(
        new_repo("shop-service", pid=str(REPO_ID), owner="acme", name="shop-service",
                 ref="app-installation:22"),
        actor=actor,
    )  # fmt: skip
    await svc.create_version("shop-service", PROFILE, actor=actor)
    await svc.publish_version("shop-service", 1, actor=actor)
    await svc.map_project(PROJECT, "shop-service", actor=actor)
    await svc.enable("shop-service", actor=actor)


async def cli(*argv: str) -> str:
    """The real CLI entry point (it calls asyncio.run, so it runs in a thread)."""

    def run() -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(list(argv), out=out, err_out=err)
        return code, out.getvalue(), err.getvalue()

    code, out, err = await asyncio.to_thread(run)
    assert code == 0, err
    return out


@asynccontextmanager
async def running_worker(settings: Settings) -> AsyncIterator[WorkerApp]:
    """One worker process (claim loop + dispatcher role), stopped cleanly on exit."""
    async with worker_app(settings) as app:
        stop = asyncio.Event()
        task = asyncio.create_task(app.worker.run(stop))
        try:
            yield app
        finally:
            stop.set()
            await asyncio.wait_for(task, 60)


def sandboxes_of(attempt_id: uuid.UUID) -> list[str]:
    return docker("ps", "-aq", "--filter", f"label=aica.attempt={attempt_id}").stdout.split()


# ---- the pipeline -------------------------------------------------------------------------------
async def test_the_whole_pipeline_survives_a_crash_a_lost_message_and_a_large_checkpoint(
    rig: Rig,
) -> None:
    started = time.monotonic()
    settings = rig.settings
    changes = build_change_service(settings, rig.inspect)
    await seed_registry(rig)

    # 1. Webhook -> API -> one DynamoDB transaction (delivery, request, job + outbox entry).
    app = create_app(settings)
    body = body_bytes(key=ISSUE_KEY, project=PROJECT, issue_id="10001", summary="Say goodbye")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://acca.test"
        ) as api,
    ):
        first = await api.post(WEBHOOK, content=body, headers=headers(body))
        again = await api.post(WEBHOOK, content=body, headers=headers(body))
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    intake = rig.job("code_change.intake")
    assert len(rig.jobs()) == 1 and intake["GSI3PK"]["S"] == "DISPATCH"  # the API never calls SQS
    request_id = uuid.UUID(json.loads(intake["payload_json"]["S"])["code_change_request_id"])
    # a duplicate SQS message for the same job, before the relay sends the real one
    rig.inspect.sqs.send_message(
        QueueUrl=rig.res.queue_url, MessageBody=json.dumps({"job_id": intake["id"]["S"]})
    )

    # 2. Worker A: relay -> claim -> intake -> prepare -> plan (big checkpoint) -> plan gate.
    async def attempt_in(status: AttemptStatus) -> Any:
        listings = [
            a for a in await changes.recent_attempts(10) if a.attempt.request_id == request_id
        ]
        return listings[0].attempt if listings and listings[0].attempt.status is status else None

    async with running_worker(settings):
        attempt = await until(
            lambda: attempt_in(AttemptStatus.AWAITING_PLAN_APPROVAL), "the plan gate"
        )

        async def intake_done() -> bool:
            return (
                rig.job_by_id(intake["id"]["S"])["status"]["S"] == "SUCCEEDED"
                and rig.queues_empty()
            )

        await until(intake_done, "the intake job and both messages to finish")
    intake_item = rig.job_by_id(intake["id"]["S"])
    assert intake_item["attempts"]["N"] == "1"  # the duplicate message never started a second run
    assert len(rig.world.model.script) == 2  # planning consumed exactly its two turns
    assert attempt.plan_sha256 and attempt.base_commit_sha

    # 3. CLI approve PLAN -> resume job (the CLI writes the job and sends it to SQS itself).
    shown = json.loads(await cli("changes", "show", str(attempt.id)))
    assert (
        shown["status"] == "AWAITING_PLAN_APPROVAL" and shown["plan_sha256"] == attempt.plan_sha256
    )
    await cli(
        "changes", "approve", str(attempt.id), "--gate", "PLAN", "--sha256", attempt.plan_sha256
    )
    resume_plan = rig.job("code_change.resume", 0)

    # 4. SIGKILL in the middle of the resume job: implementation run started, sandbox running.
    world_file = dump(rig.document, rig.bare, rig.res.root / "world.json")
    crashed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.e2e.crash_worker", str(world_file)],
        cwd=REPO, capture_output=True, text=True, timeout=180, check=False,
    )  # fmt: skip
    assert crashed.returncode == -signal.SIGKILL, crashed.stdout[-3000:]
    assert "model-called" in crashed.stdout
    held = rig.job_by_id(resume_plan["id"]["S"])
    assert (held["status"]["S"], held["attempts"]["N"]) == ("RUNNING", "1")
    assert (await changes.get_attempt(attempt.id)).status is AttemptStatus.IMPLEMENTING
    rig.attempts.append(attempt.id)  # teardown removes any sandbox of it, whatever happens
    orphans = sandboxes_of(attempt.id)
    assert len(orphans) == 1  # the dead worker's implementation sandbox is still running

    # 5. Worker B (a restart): the lease expires, the same job is reclaimed and completes once.
    async with running_worker(settings):
        attempt = await until(
            lambda: attempt_in(AttemptStatus.AWAITING_FINAL_APPROVAL), "the final gate", 180
        )

        async def resumed() -> bool:
            return bool(rig.job_by_id(resume_plan["id"]["S"])["status"]["S"] == "SUCCEEDED")

        await until(resumed, "the reclaimed resume job")
    done = rig.job_by_id(resume_plan["id"]["S"])
    assert (done["status"]["S"], done["attempts"]["N"]) == ("SUCCEEDED", "2")
    assert sandboxes_of(attempt.id) == []  # crash recovery removed the orphan with its workspace
    assert rig.world.model.script == []
    assert attempt.manifest_sha256

    # 6. CLI approve FINAL, then the message is deleted out of band: the sweep must re-send it.
    await cli(
        "changes",
        "approve",
        str(attempt.id),
        "--gate",
        "FINAL",
        "--sha256",
        attempt.manifest_sha256,
    )
    resume_final = rig.job("code_change.resume", 1)
    got = rig.inspect.sqs.receive_message(
        QueueUrl=rig.res.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=5
    )["Messages"]
    assert json.loads(got[0]["Body"]) == {"job_id": resume_final["id"]["S"]}
    rig.inspect.sqs.delete_message(
        QueueUrl=rig.res.queue_url, ReceiptHandle=got[0]["ReceiptHandle"]
    )
    assert depth(rig.inspect, rig.res.queue_url) == 0
    assert rig.job_by_id(resume_final["id"]["S"])["GSI3PK"]["S"] == "SWEEP"

    async with running_worker(settings):
        attempt = await until(lambda: attempt_in(AttemptStatus.COMPLETED), "delivery", 120)

        async def settled() -> bool:
            jobs = rig.jobs()
            return all(j["status"]["S"] == "SUCCEEDED" for j in jobs) and rig.queues_empty()

        await until(settled, "every job SUCCEEDED and both queues empty")
    elapsed = time.monotonic() - started

    # ---- assertions -----------------------------------------------------------------------------
    jobs = rig.jobs()
    assert sorted(j["kind"]["S"] for j in jobs) == [
        "code_change.intake",
        *["code_change.resume"] * 2,
    ]
    assert all(j["status"]["S"] == JobStatus.SUCCEEDED.value for j in jobs)
    assert all("GSI3PK" not in j for j in jobs)  # terminal jobs leave the outbox index
    assert rig.queues_empty()
    assert [a.attempt.id for a in await changes.recent_attempts(10)] == [attempt.id]

    # delivery: exactly one draft PR whose commit holds the validated change
    hub = rig.world.hub
    assert len(hub.pulls) == 1
    (pr,) = hub.pulls.values()
    assert pr["draft"] is True and pr["base"]["ref"] == "main"
    head = hub.ref(pr["head"]["ref"])
    assert (
        head is not None and hub.git("show", f"{head}:app/greeting.py").encode() + b"\n" == CHANGED
    )
    assert hub.foreign_requests == [] and set(rig.world.jira.calls) == {
        f"/rest/api/3/issue/{ISSUE_KEY}"
    }

    # artifacts in S3: every blob re-hashes to its metadata, and the attempt's hashes are among them
    s3 = rig.inspect.s3
    prefix = f"artifacts/{request_id}/{attempt.id}/"
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket=rig.res.bucket, Prefix=prefix) for o in p.get("Contents", [])]  # fmt: skip
    metas = [json.loads(s3.get_object(Bucket=rig.res.bucket, Key=k)["Body"].read()) for k in keys if k.endswith(".meta.json")]  # fmt: skip
    for meta in metas:
        blob = s3.get_object(Bucket=rig.res.bucket, Key=f"{prefix}{meta['id']}.blob")["Body"].read()
        assert hashlib.sha256(blob).hexdigest() == meta["sha256"] and len(blob) == meta["size"]
    kinds = {m["kind"] for m in metas}
    assert {
        "implementation_plan",
        "changeset",
        "validation_report",
        "delivery_manifest",
        "delivery_result",
    } <= kinds
    hashes = {m["sha256"] for m in metas}
    assert attempt.plan_sha256 in hashes and attempt.manifest_sha256 in hashes

    # checkpoints: the parent workflow and every agent run are in the checkpoint table
    threads = await changes.agent_threads(attempt.id)
    planning = [t for t in threads if ":planning:" in t]
    implementation = [t for t in threads if ":implementation:" in t]
    assert len(planning) == 1 and len(implementation) == 2  # the crashed run and its re-run
    pks = {i["PK"]["S"] for i in scan(rig.inspect, rig.res.checkpoint_table)}
    assert f"CHECKPOINT_attempt:{attempt.id}" in pks
    assert {f"CHECKPOINT_{t}" for t in [planning[0], implementation[1]]} <= pks
    # the planning checkpoint (> 400 KB) was offloaded to S3 and a fresh saver reads it back intact
    offloaded = [o for p in s3.get_paginator("list_objects_v2").paginate(Bucket=rig.res.bucket, Prefix=CHECKPOINT_S3_PREFIX) for o in p.get("Contents", [])]  # fmt: skip
    assert any(o["Size"] > OFFLOAD_THRESHOLD and planning[0] in o["Key"] for o in offloaded)
    # The agent's history is rebuilt from the checkpoint table plus the offloaded payload: a fresh
    # saver (another process's view) hands the planning graph its state back, big message included.
    fresh = dynamodb_checkpointer(AwsClients(settings), settings)
    graph = CodingAgent(ScriptedChatModel(script=[])).build(
        AgentPhase.PLANNING,
        DockerSandbox(name="aica-sbx-00000000-000000000000", work=rig.res.root, writable=False,
                      limits=SandboxLimits(), uid=1, gid=1),
        fresh,
        planning[0],
    )  # fmt: skip
    state = await graph.aget_state({"configurable": {"thread_id": planning[0]}})
    contents = [m.content for m in state.values["messages"]]
    assert BIG in contents and state.values["structured_response"].summary == "Return Goodbye"

    # usage ledger: one row per answered model call, provider "fake" (the killed call never answered)
    rows = await DynamoUsageStore(app_table(settings, rig.inspect)).rows(issue_keys=[ISSUE_KEY])
    assert len(rows) == 4 and {r.record.provider for r in rows} == {LEDGER_PROVIDER}

    summary = rig.tally.summary()
    print(
        f"\nE2E runtime {elapsed:.1f}s; sandboxes left by the SIGKILL: {len(orphans)} "
        "(removed by the restarted worker's crash recovery)"
    )
    print(json.dumps(summary, indent=2))
    print(rig.tally.table())
    assert summary["wru"] > 0 and summary["rru"] > 0
    assert elapsed < 600


# ---- poison messages ----------------------------------------------------------------------------
@pytest.mark.parametrize("resources", [{"max_receive_count": 2}], indirect=True)
async def test_poison_messages_go_to_the_dlq_and_the_worker_keeps_running(rig: Rig) -> None:
    settings = rig.settings.model_copy(update={"job_lease_seconds": 2, "job_heartbeat_seconds": 1})
    sqs, url = rig.inspect.sqs, rig.res.queue_url
    sqs.send_message(QueueUrl=url, MessageBody="not a job message")
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"job_id": str(uuid.uuid4())}))  # no job
    async with running_worker(settings) as app:

        async def dead_lettered() -> bool:
            return depth(rig.inspect, rig.res.dlq_url) == 2 and depth(rig.inspect, url) == 0

        await until(dead_lettered, "both poison messages in the DLQ", 60)
        assert app.worker is not None
    assert rig.jobs() == []  # nothing was created or run for them
