"""Per-test DynamoDB tables on the local Floci emulator.

A table is created from `infra/floci/schema/<schema>.json` (the same `CreateTable` input the bootstrap
uses) under a unique `s2116-<uuid>` name and deleted afterwards. Clients come from the application's
`AwsClients` with test Settings (explicit loopback endpoint, static `test`/`test` credentials); the
fixture also sets `AWS_PROFILE` and `AWS_ENDPOINT_URL` to unusable values, proving they are ignored."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import Table
from tests.support.floci import unique_name

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "infra" / "floci" / "schema"


def schema(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
    return document


def local_clients(endpoint: str) -> AwsClients:
    return AwsClients(Settings(_env_file=None, app_env="test", ACCA_AWS_ENDPOINT_URL=endpoint))


def create_table(client: Any, schema_name: str = "acca-app") -> str:
    name = unique_name("s2116")
    client.create_table(TableName=name, **schema(schema_name))
    client.get_waiter("table_exists").wait(
        TableName=name, WaiterConfig={"Delay": 1, "MaxAttempts": 30}
    )
    return name


@pytest.fixture
def app_table(floci: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Table]:
    """A fresh application table (schema `acca-app`), deleted after the test."""
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    aws = local_clients(floci)
    name = create_table(aws.dynamodb)
    try:
        yield Table(aws.dynamodb, name)
    finally:
        aws.dynamodb.delete_table(TableName=name)
