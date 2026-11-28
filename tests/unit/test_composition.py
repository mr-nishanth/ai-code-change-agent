"""The composition root on DynamoDB, SQS and S3 (no network: botocore Stubber only).

Settings point at the local emulator (explicit loopback endpoint, static `test`/`test`
credentials); nothing here sends a request. Against Floci see
`tests/integration/floci/test_composition_floci.py`."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.application.webhooks.receive import ReceiveJiraWebhook
from ai_code_change_agent.composition import (
    app_table,
    build_api,
    build_artifact_service,
    build_change_service,
    build_job_queue,
    build_readiness_probe,
    build_registry_service,
    build_repository_resolver,
    build_usage_store,
    registry_uow_factory,
    require_tables,
    worker_app,
)
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.execution.s3_artifacts import S3ArtifactStore
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import PersistenceError
from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore
from ai_code_change_agent.persistence.dynamodb.webhook_repository import DynamoWebhookRepository
from tests.support.jira import PROJECT, SECRET

ROOT = Path(__file__).resolve().parents[2]
LOCAL = "http://127.0.0.1:4566"
TABLE = "s2116-composition"
NOT_FOUND_TEXT = "Requested resource not found: Table: s2116-secret-name not found"


def floci_settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "ACCA_AWS_ENDPOINT_URL": LOCAL,
        "ACCA_DYNAMODB_TABLE_NAME": TABLE,
        "ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME": "s2116-composition-ckpt",
        "ACCA_SQS_JOB_QUEUE_NAME": "s2116-composition-jobs",
        "ACCA_S3_BUCKET_NAME": "s2116-composition",
    }
    return Settings(_env_file=None, **(base | kw))  # type: ignore[arg-type]


def describe(status: str = "ACTIVE", name: str = TABLE) -> dict[str, Any]:
    return {"Table": {"TableName": name, "TableStatus": status}}


def test_without_the_local_endpoint_composition_cannot_even_get_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    with pytest.raises(ConfigurationError, match="ACCA_AWS_ENDPOINT_URL"):
        Settings(_env_file=None, app_env="development")


def test_every_builder_wires_dynamodb_sqs_and_s3_from_aws_clients() -> None:
    s = floci_settings()
    aws = AwsClients(s)
    assert app_table(s, aws).name == TABLE
    uow = registry_uow_factory(s, aws)()
    assert isinstance(uow, DynamoRegistryUnitOfWork)
    assert build_registry_service(s, aws) is not None
    assert build_repository_resolver(s, aws) is not None
    assert build_change_service(s, aws) is not None
    assert isinstance(build_usage_store(s, aws), DynamoUsageStore)
    queue = build_job_queue(s, aws)
    assert isinstance(queue, DynamoSqsJobQueue)
    assert queue._table._client is aws.dynamodb and queue._sqs is aws.sqs
    # the lease renewal (J4): its own bounded clients, a single attempt, the same table
    assert (
        queue._beat_table._client is aws.dynamodb_heartbeat and queue._beat_sqs is aws.sqs_heartbeat
    )
    assert queue._beat_table.name == TABLE and queue._beat_table.retry.attempts == 1
    artifacts = build_artifact_service(s, aws)
    assert isinstance(artifacts._store, S3ArtifactStore)
    for client in (aws.dynamodb, aws.sqs, aws.s3):
        assert client.meta.endpoint_url == LOCAL


def test_the_api_builds_the_dynamodb_webhook_repository_and_no_sqs_or_s3_client() -> None:
    s = floci_settings(jira_webhook_secret=SECRET, jira_project_keys=PROJECT)
    aws = AwsClients(s)
    api = build_api(s, aws)
    assert isinstance(api.webhook_service, ReceiveJiraWebhook)
    repository = api.webhook_service._repository
    assert isinstance(repository, DynamoWebhookRepository)
    assert repository._table.name == TABLE
    created = set(vars(aws))
    assert "dynamodb" in created and "sqs" not in created and "s3" not in created


def test_the_api_without_webhook_configuration_has_readiness_only() -> None:
    api = build_api(floci_settings())
    assert api.webhook_service is None and api.readiness is not None


def _probe_with(aws: AwsClients) -> tuple[Any, Stubber]:
    client = aws.dynamodb_probe(1.5)
    aws.dynamodb_probe = lambda _timeout: client  # type: ignore[method-assign,assignment]
    return client, Stubber(client)


async def test_readiness_is_describe_table_on_the_app_table_with_its_own_bounded_client() -> None:
    s = floci_settings(readiness_timeout_seconds=1.5)
    aws = AwsClients(s)
    client, stub = _probe_with(aws)
    config = client.meta.config
    assert (config.connect_timeout, config.read_timeout) == (1.5, 1.5)
    assert config.retries["total_max_attempts"] == 1
    probe = build_readiness_probe(s, aws)
    with stub:
        stub.add_response("describe_table", describe(), {"TableName": TABLE})
        stub.add_response("describe_table", describe("CREATING"), {"TableName": TABLE})
        stub.add_client_error("describe_table", "ResourceNotFoundException", NOT_FOUND_TEXT, 400)
        assert await probe() is True
        assert await probe() is False
        with pytest.raises(PersistenceError) as missing:
            await probe()
        stub.assert_no_pending_responses()
    assert str(missing.value) == "ResourceNotFoundException"
    assert missing.value.__cause__ is None and "s2116-secret-name" not in repr(missing.value)


async def test_the_worker_refuses_to_start_without_its_tables() -> None:
    s = floci_settings()
    aws = AwsClients(s)
    with Stubber(aws.dynamodb) as stub:
        stub.add_response("describe_table", describe(), {"TableName": TABLE})
        stub.add_client_error("describe_table", "ResourceNotFoundException", NOT_FOUND_TEXT, 400)
        with pytest.raises(ConfigurationError) as refused:
            await require_tables(s, aws)
    assert "s2116-composition-ckpt" in str(refused.value)
    assert "ResourceNotFoundException" in str(refused.value)
    assert "s2116-secret-name" not in str(refused.value)
    with Stubber(aws.dynamodb) as stub:
        stub.add_response("describe_table", describe("DELETING"), {"TableName": TABLE})
        with pytest.raises(ConfigurationError, match="DELETING"):
            await require_tables(s, aws)


async def test_the_worker_fails_fast_on_missing_jira_or_github_configuration(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="JIRA_BASE_URL"):
        async with worker_app(floci_settings()):
            pass  # pragma: no cover - never entered
    jira = {
        "jira_base_url": "https://x.atlassian.net",
        "jira_email": "a@b.c",
        "jira_api_token": "t",
    }
    with pytest.raises(ConfigurationError, match="GitHub App"):
        async with worker_app(floci_settings(**jira)):
            pass  # pragma: no cover - never entered


def test_composition_main_and_cli_have_no_sql_left() -> None:
    result = subprocess.run(
        [  # noqa: S607
            "git", "grep", "-n", r"Postgres\|SqlAlchemy\|Sql[A-Z]", "--",
            "src/ai_code_change_agent/composition.py", "src/ai_code_change_agent/main.py",
            "src/ai_code_change_agent/cli",
        ],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )  # fmt: skip
    assert result.returncode == 1 and result.stdout == "", result.stdout
    for path in ("composition.py", "main.py", "api/routes/health.py"):
        text = (ROOT / "src" / "ai_code_change_agent" / path).read_text()
        for word in ("sqlalchemy", "DATABASE_URL", "database_url", "create_engine"):
            assert word not in text, (path, word)
