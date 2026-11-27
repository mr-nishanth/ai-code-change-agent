"""The operator CLI on DynamoDB, SQS and S3 against Floci (default composition, no injection).

Ported from the CLI parts of `tests/integration/test_registry_database.py` and
`tests/integration/test_workflow_persistence_db.py`. Each test points the CLI's Settings at its own
`s2116-<uuid>` table and job queue (+ DLQ) through `ACCA_*` variables with an explicit loopback
endpoint; `AWS_PROFILE` and `AWS_ENDPOINT_URL` are set to unusable values (by `app_table`) to prove the
CLI never reads them."""

import asyncio
import io
import json
import uuid
from collections.abc import Iterator

import pytest

from ai_code_change_agent.cli.main import main
from ai_code_change_agent.core.config import get_settings
from ai_code_change_agent.persistence.dynamodb import Table, keys
from tests.integration.floci.test_change_store_dynamo import PLAN, attempt_at_plan_approval
from tests.integration.floci.test_usage_dynamo import ledger
from tests.support import cost_ledger as fx
from tests.support.dynamo import JobQueues
from tests.support.floci import unique_name

pytestmark = [pytest.mark.floci, pytest.mark.integration]


def _env(monkeypatch: pytest.MonkeyPatch, floci: str, table: str, queue: str) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", floci)
    monkeypatch.setenv("ACCA_DYNAMODB_TABLE_NAME", table)
    monkeypatch.setenv("ACCA_SQS_JOB_QUEUE_NAME", queue)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def cli_env(
    floci: str, app_table: Table, job_queues: JobQueues, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    _env(monkeypatch, floci, app_table.name, job_queues.url.rstrip("/").rsplit("/", 1)[-1])
    try:
        yield
    finally:
        get_settings.cache_clear()


def cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err_out=err)
    return code, out.getvalue(), err.getvalue()


async def acli(*argv: str) -> tuple[int, str, str]:
    """The CLI calls asyncio.run, which cannot nest inside the test's loop: run it in a thread."""
    return await asyncio.to_thread(cli, *argv)


@pytest.mark.usefixtures("cli_env")
def test_an_empty_table_lists_no_repositories() -> None:
    assert cli("repositories", "list") == (0, "no repositories\n", "")


@pytest.mark.usefixtures("cli_env")
def test_register_map_enable_show_and_resolve(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = cli(
        "repositories", "register", "--key", "shop-api", "--display-name", "Shop API",
        "--provider", "github", "--provider-id", "4242", "--owner", "acme", "--name", "shop-api",
        "--branch", "main", "--access-ref", "app-installation:1", "--project", "shop", "--enable",
    )  # fmt: skip
    assert code == 0 and err == "", err
    assert "registered shop-api" in out and "mapped to SHOP" in out and "ACTIVE" in out
    assert cli("repositories", "map-project", "OPS", "shop-api")[1] == (
        "project OPS -> shop-api: ACTIVE\n"
    )
    code, out, _ = cli("repositories", "show", "shop-api")
    assert code == 0
    assert "shop-api: ACTIVE" in out and "version 1: published (ACTIVE)" in out
    assert "project SHOP: ACTIVE" in out and "project OPS: ACTIVE" in out
    code, out, _ = cli("--json", "repositories", "mappings", "--repository", "shop-api")
    assert code == 0 and sorted(m["jira_project_key"] for m in json.loads(out)) == ["OPS", "SHOP"]
    code, out, _ = cli("repositories", "resolve", "--project", "SHOP")
    assert code == 0 and out.startswith("RESOLVED") and "shop-api v1" in out
    code, out, _ = cli("repositories", "audit", "--repository", "shop-api")
    assert "repository.registered" in out and "cli:" in out
    code, _, err = cli("repositories", "show", "missing-repo")
    assert code == 1 and "not_found" in err
    assert capsys.readouterr().out == ""  # nothing leaks to the real stdout


async def _job(table: Table, job_id: str) -> dict[str, object]:
    item = await table.get(keys.job(uuid.UUID(job_id)))
    assert item is not None
    return item


@pytest.mark.usefixtures("cli_env")
async def test_changes_approve_is_hash_bound_and_sends_the_resume_job(
    app_table: Table, job_queues: JobQueues
) -> None:
    _, aid = await attempt_at_plan_approval(app_table)
    code, _, err = await acli(
        "changes", "approve", str(aid), "--gate", "PLAN", "--sha256", "b" * 64
    )
    assert code == 1 and "artifact_changed" in err  # a stale hash is refused, nothing enqueued
    code, out, err = await acli("changes", "approve", str(aid), "--gate", "PLAN", "--sha256", PLAN)
    assert code == 0 and err == "" and out.startswith(f"APPROVED PLAN for attempt {aid}")
    code, out, _ = await acli("changes", "show", str(aid))
    shown = json.loads(out)
    assert code == 0 and shown["attempt_id"] == str(aid)
    assert [
        (a["gate"], a["decision"], a["sha256"], a["actor_verified"]) for a in shown["approvals"]
    ] == [("PLAN", "APPROVED", PLAN, False)]
    assert shown["approvals"][0]["actor"].startswith("cli:")
    # sent directly to SQS by the CLI (worker-side producer); exactly one message
    messages = job_queues.sqs.receive_message(
        QueueUrl=job_queues.url, MaxNumberOfMessages=10, WaitTimeSeconds=1
    ).get("Messages", [])
    assert len(messages) == 1
    job = await _job(app_table, json.loads(messages[0]["Body"])["job_id"])
    assert job["kind"] == "code_change.resume" and job["status"] == "QUEUED"
    assert json.loads(str(job["payload_json"])) == {"attempt_id": str(aid)}
    assert job["GSI3PK"] == keys.GSI_SWEEP  # sent: the relay will not send it again


async def test_without_the_queue_the_resume_job_waits_for_the_relay(
    floci: str, app_table: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, floci, app_table.name, unique_name("s2116-missing"))
    try:
        _, aid = await attempt_at_plan_approval(app_table)
        code, out, err = await acli(
            "changes", "approve", str(aid), "--gate", "PLAN", "--sha256", PLAN
        )
    finally:
        get_settings.cache_clear()
    assert code == 0 and err == "" and "APPROVED" in out
    rows = await app_table.query(
        key_condition="GSI3PK = :p", values={":p": keys.GSI_DISPATCH}, index="GSI3"
    )
    jobs = [await app_table.get(keys.Key(str(r["PK"]), str(r["SK"]))) for r in rows]  # KEYS_ONLY
    assert [(j["kind"], json.loads(str(j["payload_json"]))) for j in jobs if j] == [
        ("code_change.resume", {"attempt_id": str(aid)})
    ]


@pytest.mark.usefixtures("cli_env")
async def test_costs_by_attempt_and_issue_from_the_dynamodb_ledger(app_table: Table) -> None:
    await ledger(app_table)
    code, out, err = await acli("costs", "--attempt", str(fx.A12.id))
    assert code == 0 and err == ""
    assert out.count("TASK COST REPORT") == 1
    for line in (
        "Jira Issue       : SHOP-1",
        f"Attempt          : 2 ({fx.A12.id})",
        "Workflow Result  : COMPLETED",
        "Model Calls      : 5",
        "Attempt LLM Cost : $1.3218 USD",
        "Task LLM Cost    : at least $1.5842 (INCOMPLETE: 1 call(s) without usage) USD",
    ):
        assert line in out, line
    code, out, err = await acli("costs", "--issue", "SHOP-2")
    assert code == 0 and err == ""
    assert out.index(f"({fx.A21.id})") < out.index(f"({fx.A22.id})")  # oldest attempt first
    assert "shop-web       : $0.0301 USD" in out
    code, out, err = await acli("costs", "--session", str(fx.SESSION_NOW))
    assert code == 0 and err == ""
    assert "Session LLM Cost : at least $1.3639 (INCOMPLETE: 1 call(s) without usage) USD" in out
