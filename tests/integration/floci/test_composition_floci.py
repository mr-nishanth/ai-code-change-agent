"""The switched composition against Floci: the API (webhook intake + readiness) and the worker.

Ported from `tests/integration/test_webhook_endpoint_db.py` (the PostgreSQL endpoint test). Each test
uses its own `s2116-<uuid>` tables, queue (+ DLQ) and bucket, deleted afterwards; Settings carry an
explicit loopback endpoint and `app_table` sets `AWS_PROFILE`/`AWS_ENDPOINT_URL` to unusable values
to prove they are ignored."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.composition import worker_app
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.main import create_app
from ai_code_change_agent.persistence.dynamodb import Table
from tests.support.dynamo import JobQueues, create_table, local_clients
from tests.support.fake_github import make_key
from tests.support.floci import FLOCI_ENDPOINT, unique_name
from tests.support.jira import EVENT, ISSUE_TYPE, PROJECT, SECRET, body_bytes, headers

pytestmark = [pytest.mark.floci, pytest.mark.integration]
URL = "/webhooks/jira"


def _settings(floci: str, **kw: Any) -> Settings:
    return Settings.model_validate(
        {"app_env": "test", "log_json": True, "ACCA_AWS_ENDPOINT_URL": floci} | kw
    )


def _items(table: Table) -> list[dict[str, Any]]:
    client = local_clients(FLOCI_ENDPOINT).dynamodb
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"TableName": table.name, "ConsistentRead": True}
    while True:
        page = client.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _count(items: list[dict[str, Any]], prefix: str) -> int:
    return sum(1 for i in items if i["PK"]["S"].startswith(prefix))


@pytest.fixture
def api(floci: str, app_table: Table) -> Iterator[TestClient]:
    cfg = _settings(
        floci,
        ACCA_DYNAMODB_TABLE_NAME=app_table.name,
        jira_webhook_secret=SECRET,
        jira_project_keys=PROJECT,
        jira_story_issue_types=ISSUE_TYPE,
        jira_webhook_event=EVENT,
    )
    with TestClient(create_app(cfg)) as client:
        yield client


def test_a_signed_webhook_is_one_dynamodb_transaction_and_the_api_is_ready(
    api: TestClient, app_table: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert api.get("/health/ready").json() == {
        "status": "ready",
        "dependencies": {"dynamodb": True},
    }
    body = body_bytes()
    r = api.post(URL, content=body, headers=headers(body))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert api.post(URL, content=body, headers=headers(body)).json()["status"] == "duplicate"
    wrong = body_bytes(project="OTHER", issue_id="2", key="OTHER-2")
    ignored = api.post(URL, content=wrong, headers=headers(wrong, delivery_id="d-2"))
    assert ignored.json()["status"] == "ignored"
    bad = api.post(
        URL, content=body, headers=headers(body, delivery_id="d-3", signature="sha256=" + "0" * 64)
    )
    assert bad.status_code == 401
    items = _items(app_table)
    assert _count(items, "REQ#") == 1
    jobs = [i for i in items if i["PK"]["S"].startswith("JOB#")]
    assert len(jobs) == 1 and jobs[0]["kind"]["S"] == "code_change.intake"
    assert jobs[0]["GSI3PK"]["S"] == "DISPATCH"  # the API never calls SQS: the relay sends it
    assert _count(items, "DELIVERY#") == 2  # the rejected delivery is not stored


def test_readiness_is_503_with_a_missing_table_and_shows_no_driver_text(
    floci: str, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = unique_name("s2116")
    with TestClient(create_app(_settings(floci, ACCA_DYNAMODB_TABLE_NAME=missing))) as client:
        r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "dependencies": {"dynamodb": False}}
    (logs,) = [line for line in capsys.readouterr().out.splitlines() if "readiness_" in line]
    assert "readiness_dynamodb_failed" in logs and "PersistenceError" in logs
    for leaked in (missing, "not found", "ResourceNotFound"):
        assert leaked not in r.text and leaked not in logs


@pytest.fixture
def worker_settings(
    floci: str, app_table: Table, job_queues: JobQueues, tmp_path: Path
) -> Iterator[Settings]:
    aws = local_clients(floci)
    checkpoints = create_table(aws.dynamodb, "acca-checkpoints")
    bucket = unique_name("s2116")
    aws.s3.create_bucket(Bucket=bucket)
    make_key(tmp_path / "app.pem")
    try:
        yield _settings(
            floci,
            ACCA_DYNAMODB_TABLE_NAME=app_table.name,
            ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME=checkpoints,
            ACCA_SQS_JOB_QUEUE_NAME=job_queues.url.rstrip("/").rsplit("/", 1)[-1],
            ACCA_S3_BUCKET_NAME=bucket,
            jira_base_url="https://jira.invalid",
            jira_email="a@b.c",
            jira_api_token="t",
            github_app_id=1,
            github_app_installation_id=2,
            github_app_private_key_path=str(tmp_path / "app.pem"),
            workspace_root=str(tmp_path / "ws"),
            artifact_root=str(tmp_path / "artifacts"),
            workspace_image="img:1",
            llm_provider="anthropic",
            llm_model="claude-test",
            anthropic_api_key="sk-test",
        )
    finally:
        for page in aws.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                aws.s3.delete_object(Bucket=bucket, Key=obj["Key"])
        aws.s3.delete_bucket(Bucket=bucket)
        aws.dynamodb.delete_table(TableName=checkpoints)


async def test_the_worker_app_enters_and_exits_cleanly_against_floci(
    worker_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    async with worker_app(worker_settings) as app:
        assert app.worker is not None and app.runner is not None
        assert app.costs.session_id is not None
    async with worker_app(worker_settings):  # a second process start on the same resources
        pass


async def test_the_worker_refuses_to_start_on_a_missing_checkpoint_table(
    worker_settings: Settings,
) -> None:
    missing = unique_name("s2116")
    cfg = worker_settings.model_copy(update={"dynamodb_checkpoint_table_name": missing})
    with pytest.raises(ConfigurationError, match=missing):
        async with worker_app(cfg):
            pass  # pragma: no cover - never entered
