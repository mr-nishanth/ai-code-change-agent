import asyncio

import pytest
from botocore.stub import Stubber  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from ai_code_change_agent.composition import build_readiness_probe
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.main import create_app

SECRETISH = "Requested resource not found: Table: hunter2-table not found"


def test_live(client: TestClient) -> None:
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_ready_503_before_startup_wires_a_probe(client: TestClient) -> None:
    r = client.get("/health/ready")  # no lifespan: no probe
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "dependencies": {"dynamodb": False}}


def _stubbed_probe(settings: Settings) -> tuple[object, Stubber]:
    aws = AwsClients(settings)
    client = aws.dynamodb_probe(settings.readiness_timeout_seconds)
    aws.dynamodb_probe = lambda _timeout: client  # type: ignore[method-assign,assignment]
    return build_readiness_probe(settings, aws), Stubber(client)


def test_ready_200_when_describe_table_reports_the_table_active(settings: Settings) -> None:
    probe, stub = _stubbed_probe(settings)
    app = create_app(settings)
    with TestClient(app) as c, stub:
        app.state.readiness = probe
        stub.add_response(
            "describe_table",
            {"Table": {"TableName": settings.dynamodb_table_name, "TableStatus": "ACTIVE"}},
            {"TableName": settings.dynamodb_table_name},
        )
        r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "dependencies": {"dynamodb": True}}


def test_ready_503_and_no_driver_text_when_the_table_is_missing(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    probe, stub = _stubbed_probe(settings)
    app = create_app(settings)
    with TestClient(app) as c, stub:
        app.state.readiness = probe
        stub.add_client_error("describe_table", "ResourceNotFoundException", SECRETISH, 400)
        r = c.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "dependencies": {"dynamodb": False}}
    logs = capsys.readouterr().out
    assert "readiness_dynamodb_failed" in logs and "PersistenceError" in logs
    for leaked in ("hunter2", "Requested resource", "ResourceNotFound"):
        assert leaked not in r.text and leaked not in logs


def test_ready_503_when_the_table_is_not_active(settings: Settings) -> None:
    probe, stub = _stubbed_probe(settings)
    app = create_app(settings)
    with TestClient(app) as c, stub:
        app.state.readiness = probe
        stub.add_response(
            "describe_table",
            {"Table": {"TableName": settings.dynamodb_table_name, "TableStatus": "CREATING"}},
            {"TableName": settings.dynamodb_table_name},
        )
        assert c.get("/health/ready").status_code == 503


def test_ready_503_within_the_readiness_timeout() -> None:
    cfg = Settings.model_validate({"app_env": "test", "readiness_timeout_seconds": 0.05})

    async def hangs() -> bool:
        await asyncio.sleep(5)
        return True  # pragma: no cover - cancelled by the timeout

    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.readiness = hangs
        r = c.get("/health/ready")
    assert r.status_code == 503


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    assert client.get("/health/live").headers["X-Request-ID"]
    assert (
        client.get("/health/live", headers={"X-Request-ID": "abc-1"}).headers["X-Request-ID"]
        == "abc-1"
    )


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert r.headers["X-Request-ID"] != "bad id\twith spaces"
