"""Per-test DynamoDB tables on the local Floci emulator.

A table is created from `infra/floci/schema/<schema>.json` (the same `CreateTable` input the bootstrap
uses) under a unique `s2116-<uuid>` name and deleted afterwards. Clients come from the application's
`AwsClients` with test Settings (explicit loopback endpoint, static `test`/`test` credentials); the
fixture also sets `AWS_PROFILE` and `AWS_ENDPOINT_URL` to unusable values, proving they are ignored."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.persistence.dynamodb import Table
from tests.support.floci import FLOCI_ENDPOINT, floci_reachable, unique_name

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


@pytest.fixture(scope="session")
def floci() -> str:
    """The Floci endpoint; every test that uses it is a SKIP with a reason when Floci is down."""
    reason = floci_reachable()
    if reason is not None:
        pytest.skip(reason)
    return FLOCI_ENDPOINT


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


@dataclass(frozen=True)
class JobQueues:
    """A per-test SQS job queue and its DLQ (`s2116-<uuid>` names), with the client that made them."""

    sqs: Any
    url: str
    dlq_url: str


def create_job_queues(sqs: Any, *, max_receive_count: int = 20) -> JobQueues:
    """The bootstrap's job queue shape (VisibilityTimeout 300, long polling 20 s, redrive to a DLQ)
    under unique names; `max_receive_count` lowered by the poison-message test."""
    dlq_url = sqs.create_queue(QueueName=unique_name("s2116-dlq"))["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    redrive = json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": str(max_receive_count)})
    url = sqs.create_queue(
        QueueName=unique_name("s2116-jobs"),
        Attributes={
            "VisibilityTimeout": "300",
            "ReceiveMessageWaitTimeSeconds": "20",
            "RedrivePolicy": redrive,
        },
    )["QueueUrl"]
    return JobQueues(sqs, url, dlq_url)


def delete_job_queues(queues: JobQueues) -> None:
    for url in (queues.url, queues.dlq_url):
        queues.sqs.delete_queue(QueueUrl=url)


@pytest.fixture
def job_queues(floci: str) -> Iterator[JobQueues]:
    """A fresh job queue + DLQ on Floci, deleted after the test."""
    queues = create_job_queues(local_clients(floci).sqs)
    try:
        yield queues
    finally:
        delete_job_queues(queues)
