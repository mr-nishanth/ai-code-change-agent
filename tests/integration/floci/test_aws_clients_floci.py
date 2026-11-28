"""Smoke test: the application's AwsClients reach the local Floci emulator (ListTables)."""

import pytest

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients

pytestmark = [pytest.mark.floci, pytest.mark.integration]


def test_application_clients_list_tables_on_floci(
    floci: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", floci)
    aws = AwsClients(Settings(_env_file=None, app_env="test"))
    tables = aws.dynamodb.list_tables()["TableNames"]
    assert isinstance(tables, list)
    assert aws.dynamodb.meta.endpoint_url == floci
