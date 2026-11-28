"""Fixtures for the local end-to-end tests: Floci (DynamoDB, SQS, S3) and Docker (sandbox, validation).

Every test gets its own `s2116-<uuid>` application and checkpoint tables (created from
`infra/floci/schema/*.json`, like the bootstrap), job queue + DLQ and bucket, all deleted afterwards,
and its own WORKSPACE_ROOT / ARTIFACT_ROOT under `tmp_path`. Clients use the explicit loopback
endpoint and the dummy `test`/`test` credentials only; `AWS_PROFILE` and `AWS_ENDPOINT_URL` are set
to unusable values to prove nothing reads them.

A test is a SKIP with a reason (never a failure) when Floci is down, Docker or the execution image is
missing, or a live sandbox (`aica.role=agent-sandbox`) is running: the live worker's sandboxes must not
share the Docker daemon with this suite (docs/DOCKER-DEBUGGING.md)."""

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.world import IMAGE_DEFAULT
from tests.support.dynamo import create_job_queues, create_table, delete_job_queues, local_clients
from tests.support.fake_github import make_key
from tests.support.floci import floci_reachable, unique_name

IMAGE = os.environ.get("WORKSPACE_IMAGE", IMAGE_DEFAULT)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/e2e/" in item.path.as_posix():
            for mark in (pytest.mark.floci, pytest.mark.docker, pytest.mark.integration):
                item.add_marker(mark)


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker argv in a test helper
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def skip_reason() -> str | None:
    reason = floci_reachable()
    if reason is not None:
        return reason
    if shutil.which("docker") is None:
        return "docker CLI not found"
    if docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        return "docker daemon not reachable"
    if docker("image", "inspect", IMAGE).returncode != 0:
        return f"execution image '{IMAGE}' is not built (docker/execution/Dockerfile)"
    live = docker("ps", "-q", "--filter", "label=aica.role=agent-sandbox").stdout.split()
    if live:
        return f"{len(live)} live agent sandbox(es) running; e2e tests share the Docker daemon"
    return None


@pytest.fixture
def e2e_ready(monkeypatch: pytest.MonkeyPatch) -> str:
    """The Floci endpoint, or a SKIP with the reason; also proves ambient AWS config is ignored."""
    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    from tests.support.floci import FLOCI_ENDPOINT

    return FLOCI_ENDPOINT


@dataclass(frozen=True)
class Resources:
    endpoint: str
    table: str
    checkpoint_table: str
    queue_url: str
    dlq_url: str
    bucket: str
    key_path: Path
    root: Path
    image: str = IMAGE

    @property
    def queue_name(self) -> str:
        return self.queue_url.rstrip("/").rsplit("/", 1)[-1]


def _empty_bucket(s3: Any, bucket: str) -> None:
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=obj["Key"])


@pytest.fixture
def resources(
    e2e_ready: str, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Resources]:
    """Fresh tables, queue + DLQ and bucket (indirect parameter: `max_receive_count`)."""
    options: dict[str, Any] = getattr(request, "param", {})
    aws = local_clients(e2e_ready)
    table = create_table(aws.dynamodb, "acca-app")
    checkpoints = create_table(aws.dynamodb, "acca-checkpoints")
    queues = create_job_queues(aws.sqs, max_receive_count=options.get("max_receive_count", 20))
    bucket = unique_name("s2116")
    aws.s3.create_bucket(Bucket=bucket)
    root = tmp_path / "e2e"
    root.mkdir()
    key_path = root / "app.pem"
    make_key(key_path)
    try:
        yield Resources(
            e2e_ready, table, checkpoints, queues.url, queues.dlq_url, bucket, key_path, root
        )
    finally:
        _empty_bucket(aws.s3, bucket)
        aws.s3.delete_bucket(Bucket=bucket)
        delete_job_queues(queues)
        for name in (table, checkpoints):
            aws.dynamodb.delete_table(TableName=name)
