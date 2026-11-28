"""The DynamoDB checkpointer's configuration (DM 9.4) and the thread id length guard."""

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import boto3  # type: ignore[import-untyped]
import pytest
from langgraph.checkpoint.serde import jsonplus
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph_checkpoint_aws.checkpoint.dynamodb import (  # type: ignore[import-untyped]
    DynamoDBSaver,
)

from ai_code_change_agent.agents.coding.schemas import (
    ImplementationPlan,
    ImplementationReport,
    PlannedChange,
)
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
    "jira_webhook_secret": "x",
    "jira_project_keys": "SHOP",
}


class RecordingSaver:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.serde: Any = None
        self.serializer = SimpleNamespace(serde=None)


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
        "ttl_seconds": 2_592_000,  # ACCA_CHECKPOINT_TTL_DAYS default: 30 days
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


def test_the_checkpoint_ttl_follows_acca_checkpoint_ttl_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        ACCA_AWS_ENDPOINT_URL="http://127.0.0.1:4566",
        ACCA_CHECKPOINT_TTL_DAYS=7,
    )
    _, kwargs = _build(monkeypatch, settings)
    assert kwargs["ttl_seconds"] == 7 * 86_400  # the saver's S3 lifecycle rule: 7 days
    assert kwargs["consistent_read"] is True  # resume correctness, never traded for cost


def _plan() -> ImplementationPlan:
    return ImplementationPlan(
        summary="Add a health endpoint",
        changes=[PlannedChange(path="app/health.py", operation="create", purpose="endpoint")],
    )


def test_the_checkpoint_serializer_allows_the_app_schema_types_explicitly() -> None:
    serde = checkpointer.checkpoint_serde()
    assert isinstance(serde, JsonPlusSerializer)
    allowed = serde._allowed_msgpack_modules  # the installed langgraph-checkpoint's allow-list
    assert allowed is not True  # not the permissive warn-and-allow default
    assert {
        (t.__module__, t.__name__)
        for t in (ImplementationPlan, ImplementationReport, PlannedChange)
    } <= set(allowed or ())


def test_the_built_saver_uses_the_allow_listed_serializer_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None, app_env="test", ACCA_AWS_ENDPOINT_URL="http://127.0.0.1:4566"
    )
    _, _kwargs = _build(monkeypatch, settings)
    saver: Any = checkpointer.dynamodb_checkpointer(AwsClients(settings), settings)
    assert isinstance(saver.serde, JsonPlusSerializer)
    assert saver.serializer.serde is saver.serde  # DynamoDBSaver copies serde at construction


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if "unregistered" in r.getMessage() or "Blocked" in r.getMessage()
    ]


def test_a_checkpoint_with_an_implementation_plan_round_trips_without_a_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real DynamoDBSaver serializer path (no AWS call: no S3 offload, nothing read)."""
    monkeypatch.setattr(jsonplus, "_warned_unregistered_types", set())  # warn-once state
    monkeypatch.setattr(jsonplus, "_warned_blocked_types", set())
    with caplog.at_level(logging.WARNING):  # control: the library default warns for the plan
        default = JsonPlusSerializer(allowed_msgpack_modules=True)
        default.loads_typed(default.dumps_typed(_plan()))
    assert any("ImplementationPlan" in m for m in _warnings(caplog))
    caplog.clear()
    monkeypatch.setattr(jsonplus, "_warned_unregistered_types", set())
    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    saver = DynamoDBSaver(
        table_name="unit-no-table", session=session, endpoint_url="http://127.0.0.1:9"
    )
    checkpointer.use_checkpoint_serde(saver)
    report = ImplementationReport(outcome="implemented", summary="done")
    value = {"channel_values": {"structured_response": _plan(), "report": report}}
    with caplog.at_level(logging.WARNING):
        type_, data, compression = saver.serializer.serialize(value)
        restored = saver.serializer.deserialize(type_, data, compression)
    assert restored["channel_values"]["structured_response"] == _plan()
    assert isinstance(restored["channel_values"]["report"], ImplementationReport)
    assert _warnings(caplog) == []
