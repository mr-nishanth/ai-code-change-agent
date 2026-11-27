"""The DynamoDB checkpointer's configuration (DM 9.4) and the thread id length guard."""

import uuid
from typing import Any

import pytest

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.orchestration.langgraph import checkpointer
from ai_code_change_agent.orchestration.langgraph.ports import (
    MAX_THREAD_ID_LENGTH,
    checked_thread_id,
)
from ai_code_change_agent.orchestration.langgraph.runner import _config

PROD = {
    "app_env": "production",
    "llm_provider": "bedrock",
    "database_url": "postgresql+psycopg://u:p@h/db",
    "jira_webhook_secret": "x",
    "jira_project_keys": "SHOP",
}


class RecordingSaver:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _build(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[AwsClients, dict[str, Any]]:
    monkeypatch.setattr(checkpointer, "DynamoDBSaver", RecordingSaver)
    clients = AwsClients(settings)
    saver: Any = checkpointer.dynamodb_checkpointer(clients, settings)
    return clients, saver.kwargs


def test_the_local_saver_uses_the_app_session_and_the_floci_endpoint_for_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        ACCA_AWS_ENDPOINT_URL="http://127.0.0.1:4566",
        ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME="s2116-ckpt",
        ACCA_S3_BUCKET_NAME="s2116-bucket",
    )
    clients, kwargs = _build(monkeypatch, settings)
    config = kwargs.pop("boto_config")
    assert config.ignore_configured_endpoint_urls is True
    assert kwargs == {
        "table_name": "s2116-ckpt",
        "session": clients.session,
        "region_name": "us-east-1",
        "endpoint_url": "http://127.0.0.1:4566",
        "ttl_seconds": 7_776_000,
        "s3_offload_config": {
            "bucket_name": "s2116-bucket",
            "key_prefix": "checkpoints/",
            "endpoint_url": "http://127.0.0.1:4566",
        },
        "consistent_read": True,
    }


def test_without_an_override_s3_offload_has_no_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL", raising=False)
    settings = Settings(_env_file=None, **PROD)  # type: ignore[arg-type]
    clients, kwargs = _build(monkeypatch, settings)
    assert kwargs["endpoint_url"] is None and kwargs["session"] is clients.session
    assert kwargs["s3_offload_config"] == {
        "bucket_name": settings.s3_bucket_name,
        "key_prefix": "checkpoints/",
    }
    assert kwargs["consistent_read"] is True


def test_thread_ids_must_stay_under_200_characters() -> None:
    assert MAX_THREAD_ID_LENGTH == 200
    ok = "t" * 199
    assert checked_thread_id(ok) == ok
    with pytest.raises(ValueError, match="200"):
        checked_thread_id("t" * 200)


def test_the_runner_builds_short_thread_ids() -> None:
    attempt = uuid.uuid4()
    assert _config(attempt) == {"configurable": {"thread_id": f"attempt:{attempt}"}}
