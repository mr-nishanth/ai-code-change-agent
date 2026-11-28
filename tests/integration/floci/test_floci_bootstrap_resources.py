"""Floci bootstrap resources (infra/floci/init, infra/floci/schema, infra/terraform/local).

The bootstrap scripts are run against the local emulator with unique `s2116-<uuid>` names, twice, and the
resulting tables, queues and bucket are compared and deleted. The scripts' endpoint guard, the compose
settings for the web console and the old variable names are checked without Floci."""

import contextlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support.floci import FLOCI_ENDPOINT, floci_client

pytestmark = [pytest.mark.floci, pytest.mark.integration]

REPO = Path(__file__).resolve().parents[3]
INIT_DIR = REPO / "infra" / "floci" / "init"
SCHEMA_DIR = REPO / "infra" / "floci" / "schema"
TERRAFORM_DIR = REPO / "infra" / "terraform" / "local"
SCRIPTS = [*sorted(p.name for p in INIT_DIR.glob("[0-9][0-9]-*.sh")), "bootstrap.sh"]


def _run(
    argv: list[str], env: dict[str, str], cwd: Path = REPO, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv in a test helper
        argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _script_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A minimal environment that also proves the scripts ignore ambient AWS settings."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "AWS_PROFILE": "does-not-exist",
        "AWS_ENDPOINT_URL": "http://203.0.113.1:1",
        "ACCA_FLOCI_ENDPOINT_URL": FLOCI_ENDPOINT,
        **extra,
    }


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("endpoint", ["http://example.com:4566", "https://127.0.0.1.nip.io"])
def test_scripts_refuse_non_loopback_endpoints(tmp_path: Path, script: str, endpoint: str) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "called"
    for tool in ("aws", "curl"):
        fake = fake_bin / tool
        fake.write_text(f"#!/bin/sh\necho {tool} >> '{marker}'\n")
        fake.chmod(0o755)
    env = _script_env(tmp_path, ACCA_FLOCI_ENDPOINT_URL=endpoint)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _run(["sh", str(INIT_DIR / script)], env)

    assert result.returncode == 2, result.stdout + result.stderr
    assert f"refusing endpoint '{endpoint}'" in result.stderr
    assert not marker.exists(), "no aws or curl call may happen before the endpoint is accepted"


def test_old_script_variable_names_are_gone() -> None:
    old = ["FLOCI_" + suffix for suffix in ("INIT_DIR", "INIT_HEALTHY", "ENDPOINT_URL")]
    argv = ["git", "grep", "-n", "-w"]
    for name in old:
        argv += ["-e", name]
    result = _run(argv, dict(os.environ))
    assert result.returncode == 1, result.stdout


def test_compose_pins_the_console_to_loopback() -> None:
    compose = yaml.safe_load((REPO / "infra" / "docker-compose.yml").read_text())
    environment = compose["services"]["floci"]["environment"]
    assert environment["FLOCI_SERVICES_UI_BIND_ADDRESS"] == "127.0.0.1"
    assert environment["FLOCI_SERVICES_UI_IMAGE"].startswith("floci/floci-ui:0.7.0@sha256:")
    assert environment["FLOCI_STORAGE_SERVICES_DYNAMODB_ITEM_LAYOUT"] == "per-table"
    assert environment["FLOCI_STORAGE_SERVICES_DYNAMODB_MODE"] == "wal"


def _describe(name: str) -> dict[str, Any]:
    ddb = floci_client("dynamodb")
    table = ddb.describe_table(TableName=name)["Table"]
    ttl = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
    return {
        "keys": table["KeySchema"],
        "attributes": sorted(
            (a["AttributeName"], a["AttributeType"]) for a in table["AttributeDefinitions"]
        ),
        "billing": table.get("BillingModeSummary", {}).get("BillingMode"),
        "gsis": sorted(
            (
                g["IndexName"],
                tuple((k["AttributeName"], k["KeyType"]) for k in g["KeySchema"]),
                g["Projection"]["ProjectionType"],
            )
            for g in table.get("GlobalSecondaryIndexes", [])
        ),
        "ttl": (ttl.get("TimeToLiveStatus"), ttl.get("AttributeName")),
    }


def _queue_attributes(name: str) -> dict[str, str]:
    sqs = floci_client("sqs")
    url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
    attributes: dict[str, str] = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])[
        "Attributes"
    ]
    return attributes


def _snapshot(app: str, checkpoints: str, queue: str, bucket: str) -> dict[str, Any]:
    jobs = _queue_attributes(queue)
    dlq = _queue_attributes(f"{queue}-dlq")
    floci_client("s3").head_bucket(Bucket=bucket)
    return {
        "app": _describe(app),
        "checkpoints": _describe(checkpoints),
        "jobs": {
            key: jobs[key]
            for key in ("VisibilityTimeout", "ReceiveMessageWaitTimeSeconds", "QueueArn")
        },
        "redrive": json.loads(jobs["RedrivePolicy"]),
        "dlq_arn": dlq["QueueArn"],
    }


def _delete_tables(*names: str) -> None:
    ddb = floci_client("dynamodb")
    for name in names:
        with contextlib.suppress(ddb.exceptions.ResourceNotFoundException):
            ddb.delete_table(TableName=name)


def _delete_queues(*names: str) -> None:
    sqs = floci_client("sqs")
    for name in names:
        with contextlib.suppress(sqs.exceptions.QueueDoesNotExist):
            sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=name)["QueueUrl"])


def _delete_bucket(name: str) -> None:
    s3 = floci_client("s3")
    with contextlib.suppress(s3.exceptions.NoSuchBucket):
        s3.delete_bucket(Bucket=name)


def test_bootstrap_creates_tables_queues_and_bucket_idempotently(
    floci: str, tmp_path: Path
) -> None:
    if shutil.which("aws") is None:
        pytest.skip("aws CLI not found (the bootstrap scripts use it)")
    prefix = f"s2116-{uuid.uuid4().hex[:12]}"
    app, checkpoints, queue, bucket = (
        f"{prefix}-app",
        f"{prefix}-checkpoints",
        f"{prefix}-jobs",
        f"{prefix}-artifacts",
    )
    env = _script_env(
        tmp_path,
        ACCA_DYNAMODB_TABLE_NAME=app,
        ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME=checkpoints,
        ACCA_SQS_JOB_QUEUE_NAME=queue,
        ACCA_S3_BUCKET_NAME=bucket,
    )
    scripts = ["10-sqs.sh", "20-s3.sh", "60-dynamodb.sh"]
    try:
        snapshots = []
        for _ in range(2):
            for script in scripts:
                result = _run(["sh", str(INIT_DIR / script)], env)
                assert result.returncode == 0, result.stdout + result.stderr
            snapshots.append(_snapshot(app, checkpoints, queue, bucket))
        first, second = snapshots
        assert first == second

        pk_sk = [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ]
        assert first["app"]["keys"] == pk_sk
        assert first["app"]["billing"] == "PAY_PER_REQUEST"
        assert {name for name, kind in first["app"]["attributes"]} == {
            "PK",
            "SK",
            "GSI1PK",
            "GSI1SK",
            "GSI2PK",
            "GSI2SK",
            "GSI3PK",
            "GSI3SK",
        }
        assert {kind for _, kind in first["app"]["attributes"]} == {"S"}
        assert first["app"]["gsis"] == [
            (f"GSI{n}", ((f"GSI{n}PK", "HASH"), (f"GSI{n}SK", "RANGE")), projection)
            for n, projection in ((1, "KEYS_ONLY"), (2, "INCLUDE"), (3, "KEYS_ONLY"))
        ]
        assert first["app"]["ttl"] == ("ENABLED", "expires_at")

        assert first["checkpoints"]["keys"] == pk_sk
        assert first["checkpoints"]["attributes"] == [("PK", "S"), ("SK", "S")]
        assert first["checkpoints"]["billing"] == "PAY_PER_REQUEST"
        assert first["checkpoints"]["gsis"] == []
        assert first["checkpoints"]["ttl"] == ("ENABLED", "ttl")

        assert first["jobs"]["VisibilityTimeout"] == "300"
        assert first["jobs"]["ReceiveMessageWaitTimeSeconds"] == "20"
        assert first["redrive"]["deadLetterTargetArn"] == first["dlq_arn"]
        assert str(first["redrive"]["maxReceiveCount"]) == "20"
    finally:
        _delete_tables(app, checkpoints)
        _delete_queues(queue, f"{queue}-dlq")
        _delete_bucket(bucket)


def test_dynamodb_script_refuses_index_drift_and_recreates_on_request(
    floci: str, tmp_path: Path
) -> None:
    """A table with an old GSI layout (projection ALL) is refused with the recreate command, then
    recreated from the schema when ACCA_DYNAMODB_RECREATE_ON_DRIFT=1 (local data only)."""
    if shutil.which("aws") is None:
        pytest.skip("aws CLI not found (the bootstrap scripts use it)")
    prefix = f"s2116-{uuid.uuid4().hex[:12]}"
    app, checkpoints = f"{prefix}-app", f"{prefix}-checkpoints"
    old = json.loads((SCHEMA_DIR / "acca-app.json").read_text())
    for index in old["GlobalSecondaryIndexes"]:
        index["Projection"] = {"ProjectionType": "ALL"}
    ddb = floci_client("dynamodb")
    ddb.create_table(TableName=app, **old)
    env = _script_env(
        tmp_path, ACCA_DYNAMODB_TABLE_NAME=app, ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME=checkpoints
    )
    try:
        refused = _run(["sh", str(INIT_DIR / "60-dynamodb.sh")], env)
        assert refused.returncode == 5, refused.stdout + refused.stderr
        assert "ACCA_DYNAMODB_RECREATE_ON_DRIFT=1" in refused.stderr
        recreated = _run(
            ["sh", str(INIT_DIR / "60-dynamodb.sh")],
            {**env, "ACCA_DYNAMODB_RECREATE_ON_DRIFT": "1"},
        )
        assert recreated.returncode == 0, recreated.stdout + recreated.stderr
        assert "recreated" in recreated.stdout
        table = ddb.describe_table(TableName=app)["Table"]
        assert {i["IndexName"]: i["Projection"] for i in table["GlobalSecondaryIndexes"]} == {
            "GSI1": {"ProjectionType": "KEYS_ONLY"},
            "GSI2": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["entity_type", "entity_id"]},
            "GSI3": {"ProjectionType": "KEYS_ONLY"},
        }
        assert table["TableClassSummary"]["TableClass"] == "STANDARD"
        again = _run(["sh", str(INIT_DIR / "60-dynamodb.sh")], env)  # idempotent once aligned
        assert again.returncode == 0 and f"dynamodb table {app} exists" in again.stdout
        names = ddb.list_tables()["TableNames"]
        assert not [n for n in names if n.startswith(prefix) and n.endswith("-schema-probe")]
    finally:
        _delete_tables(app, checkpoints)


def test_sqs_script_updates_an_existing_queue(floci: str, tmp_path: Path) -> None:
    if shutil.which("aws") is None:
        pytest.skip("aws CLI not found (the bootstrap scripts use it)")
    queue = f"s2116-{uuid.uuid4().hex[:12]}-jobs"
    sqs = floci_client("sqs")
    sqs.create_queue(QueueName=queue, Attributes={"VisibilityTimeout": "30"})
    try:
        result = _run(
            ["sh", str(INIT_DIR / "10-sqs.sh")],
            _script_env(tmp_path, ACCA_SQS_JOB_QUEUE_NAME=queue),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        attributes = _queue_attributes(queue)
        assert attributes["VisibilityTimeout"] == "300"
        assert attributes["ReceiveMessageWaitTimeSeconds"] == "20"
        assert str(json.loads(attributes["RedrivePolicy"])["maxReceiveCount"]) == "20"
    finally:
        _delete_queues(queue, f"{queue}-dlq")


def test_schema_files_are_valid_create_table_input(floci: str) -> None:
    """The schema files are CreateTable input without TableName (the caller supplies the name)."""
    ddb = floci_client("dynamodb")
    for schema_file in sorted(SCHEMA_DIR.glob("*.json")):
        schema = json.loads(schema_file.read_text())
        assert "TableName" not in schema
        name = f"s2116-{uuid.uuid4().hex[:12]}"
        ddb.create_table(TableName=name, **schema)
        try:
            assert ddb.describe_table(TableName=name)["Table"]["TableStatus"] in {
                "CREATING",
                "ACTIVE",
            }
        finally:
            ddb.delete_table(TableName=name)


def test_terraform_configuration_is_valid(tmp_path: Path) -> None:
    if shutil.which("terraform") is None:
        pytest.skip("terraform CLI not found")
    env = {**os.environ, "TF_DATA_DIR": str(tmp_path / "tf-data"), "TF_IN_AUTOMATION": "1"}
    init = _run(
        ["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"],
        env,
        cwd=TERRAFORM_DIR,
        timeout=300,
    )
    if init.returncode != 0:
        pytest.skip(f"terraform init failed (offline?): {init.stderr.strip()[-300:]}")
    validate = _run(["terraform", "validate", "-no-color"], env, cwd=TERRAFORM_DIR)
    assert validate.returncode == 0, validate.stdout + validate.stderr
    assert "The configuration is valid" in validate.stdout
