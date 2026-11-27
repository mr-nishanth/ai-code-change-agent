"""The API process: starts with no database URL, builds only DynamoDB (no SQS or S3 client)."""

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.application.webhooks.receive import ReceiveJiraWebhook
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.main import create_app
from tests.support.jira import PROJECT, SECRET


def _never(self: AwsClients) -> None:
    raise AssertionError("the API must not create this client")


@pytest.fixture(autouse=True)
def _no_sqs_or_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(AwsClients, "sqs", property(_never))
    monkeypatch.setattr(AwsClients, "s3", property(_never))


def test_the_api_starts_without_a_database_url_and_wires_the_webhook_on_dynamodb() -> None:
    cfg = Settings.model_validate(
        {"app_env": "test", "jira_webhook_secret": SECRET, "jira_project_keys": PROJECT}
    )
    assert not hasattr(cfg, "database_url")
    app = create_app(cfg)
    with TestClient(app):
        assert isinstance(app.state.webhook_service, ReceiveJiraWebhook)
        assert app.state.readiness is not None
        assert not hasattr(app.state, "engine")


def test_without_webhook_configuration_the_api_serves_health_only() -> None:
    app = create_app(Settings.model_validate({"app_env": "test"}))
    with TestClient(app) as client:
        assert app.state.webhook_service is None
        assert client.get("/health/live").status_code == 200
