"""Floci coverage for the container-backed services: ECR, ECS, RDS, Lambda and API Gateway.

These tests make Floci start real sibling containers (registry, ECS tasks, PostgreSQL, Lambda
runtimes), so they need Docker as well as Floci and skip with a reason when either is missing or a
required image cannot be pulled. Every resource is uniquely named and deleted in `finally`; every
wait is bounded. `docker login` writes only to a temporary DOCKER_CONFIG, never to ~/.docker."""

import base64
import io
import json
import os
import re
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from tests.support.floci import FLOCI_ENDPOINT, floci_client, unique_name, wait_until

pytestmark = [pytest.mark.floci, pytest.mark.docker, pytest.mark.integration]

SMALL_IMAGE = "busybox:1.36"
RDS_POSTGRES_IMAGE = "postgres:16-alpine"  # Floci 2.5.0 default for engine=postgres
LAMBDA_PYTHON_IMAGE = "public.ecr.aws/lambda/python:3.12"  # Floci's python3.12 runtime image
LAMBDA_ROLE = "arn:aws:iam::000000000000:role/lambda-role"
RDS_PORTS = range(7001, 7006)  # FLOCI_SERVICES_RDS_PROXY_BASE_PORT..MAX_PORT
DIGEST = re.compile(r"digest: (sha256:[0-9a-f]{64})")


def _docker(
    *args: str, env: dict[str, str] | None = None, stdin: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker argv in a test helper
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
        timeout=timeout,
        check=False,
    )


def _ensure_image(ref: str) -> None:
    """Use a cached image or pull it; skip (not fail) when it cannot be pulled."""
    if _docker("image", "inspect", ref, timeout=30).returncode == 0:
        return
    try:
        pulled = _docker("pull", ref, timeout=600)
    except subprocess.TimeoutExpired:
        pytest.skip(f"pulling {ref} timed out")
    if pulled.returncode != 0:
        pytest.skip(f"cannot pull {ref}: {pulled.stdout[-300:]}{pulled.stderr[-300:]}")


@pytest.fixture(scope="module")
def small_image(floci: str, docker_ready: None) -> str:
    _ensure_image(SMALL_IMAGE)
    return SMALL_IMAGE


@pytest.fixture
def isolated_docker_env(tmp_path: Path, docker_ready: None) -> dict[str, str]:
    """Docker CLI environment whose credentials live in a throwaway config dir.

    The current context's daemon address is kept explicitly, because a fresh DOCKER_CONFIG has no
    contexts and would otherwise fall back to the default socket."""
    env = dict(os.environ)
    if "DOCKER_HOST" not in env:
        host = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}", timeout=30)
        if host.returncode == 0 and host.stdout.strip():
            env["DOCKER_HOST"] = host.stdout.strip()
    config_dir = tmp_path / "docker-config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}")
    env["DOCKER_CONFIG"] = str(config_dir)
    return env


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


# --- ECR -------------------------------------------------------------------------------------------


def test_ecr_push_and_pull_by_digest(small_image: str, isolated_docker_env: dict[str, str]) -> None:
    ecr = floci_client("ecr")
    name = unique_name("acca-ecr")
    repo_uri = ecr.create_repository(repositoryName=name)["repository"]["repositoryUri"]
    tagged = f"{repo_uri}:v1"
    pulled_ref: str | None = None
    try:
        registry = repo_uri.split("/", 1)[0]
        assert registry.split(":")[0] in {"localhost", "127.0.0.1"}  # path-style, loopback only
        token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
        user, password = base64.b64decode(token).decode().split(":", 1)
        login = _docker(
            "login",
            "--username",
            user,
            "--password-stdin",
            registry,
            env=isolated_docker_env,
            stdin=password,
            timeout=60,
        )
        assert login.returncode == 0, login.stderr

        assert _docker("tag", small_image, tagged).returncode == 0
        push = _docker("push", tagged, env=isolated_docker_env, timeout=300)
        assert push.returncode == 0, push.stdout + push.stderr
        match = DIGEST.search(push.stdout)
        assert match is not None, push.stdout
        digest = match.group(1)

        described = ecr.describe_images(repositoryName=name, imageIds=[{"imageTag": "v1"}])
        assert described["imageDetails"][0]["imageDigest"] == digest
        fetched = ecr.batch_get_image(repositoryName=name, imageIds=[{"imageTag": "v1"}])
        assert not fetched["failures"]
        assert fetched["images"][0]["imageId"]["imageDigest"] == digest

        assert _docker("rmi", tagged).returncode == 0
        pulled_ref = f"{repo_uri}@{digest}"
        pull = _docker("pull", pulled_ref, env=isolated_docker_env, timeout=300)
        assert pull.returncode == 0, pull.stdout + pull.stderr
        assert digest in pull.stdout
    finally:
        _docker("rmi", tagged)
        if pulled_ref is not None:
            _docker("rmi", pulled_ref)
        ecr.delete_repository(repositoryName=name, force=True)


# --- ECS -------------------------------------------------------------------------------------------


def _task(ecs: Any, cluster: str, task_arn: str) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"]
    assert len(tasks) == 1
    return tasks[0]


def _wait_status(ecs: Any, cluster: str, task_arn: str, status: str) -> dict[str, Any]:
    def reached() -> dict[str, Any] | None:
        task = _task(ecs, cluster, task_arn)
        return task if task["lastStatus"] == status else None

    return wait_until(reached, timeout=120, interval=1.0)


def _ecs_containers() -> list[str]:
    listed = _docker("ps", "-a", "--filter", "label=io.floci.service=ecs", "--format", "{{.Names}}")
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()


def test_ecs_task_exit_code_and_stop(small_image: str) -> None:
    ecs = floci_client("ecs")
    cluster = unique_name("acca-ecs")
    ecs.create_cluster(clusterName=cluster)
    families: list[str] = []
    running: list[str] = []

    def register(command: list[str]) -> str:
        family = unique_name("acca-task")
        definition = ecs.register_task_definition(
            family=family,
            requiresCompatibilities=["EC2"],
            networkMode="bridge",
            containerDefinitions=[
                {
                    "name": "main",
                    "image": small_image,
                    "command": command,
                    "essential": True,
                    "memory": 64,
                }
            ],
        )["taskDefinition"]
        families.append(definition["taskDefinitionArn"])
        return str(definition["taskDefinitionArn"])

    def run(task_definition: str) -> str:
        result = ecs.run_task(cluster=cluster, taskDefinition=task_definition, launchType="EC2")
        assert not result["failures"], result["failures"]
        arn = str(result["tasks"][0]["taskArn"])
        running.append(arn)
        return arn

    try:
        exiting = run(register(["sh", "-c", "exit 7"]))
        stopped = _wait_status(ecs, cluster, exiting, "STOPPED")
        assert stopped["containers"][0]["exitCode"] == 7

        sleeping = run(register(["sleep", "300"]))
        _wait_status(ecs, cluster, sleeping, "RUNNING")
        ecs.stop_task(cluster=cluster, task=sleeping, reason="acca floci test stop")
        stopped = _wait_status(ecs, cluster, sleeping, "STOPPED")
        assert stopped["stoppedReason"]
    finally:
        for arn in running:
            if _task(ecs, cluster, arn)["lastStatus"] != "STOPPED":
                ecs.stop_task(cluster=cluster, task=arn, reason="acca floci test cleanup")
                _wait_status(ecs, cluster, arn, "STOPPED")
        for arn in families:
            ecs.deregister_task_definition(taskDefinition=arn)
        ecs.delete_cluster(cluster=cluster)
    wait_until(lambda: not _ecs_containers(), timeout=60, interval=1.0)


# --- RDS -------------------------------------------------------------------------------------------


def _rds_volumes(resource_id: str) -> list[str]:
    listed = _docker(
        "volume", "ls", "--filter", f"name=floci-aws-rds-{resource_id}", "--format", "{{.Name}}"
    )
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.split()


def test_rds_postgres_create_connect_query_delete(floci: str, docker_ready: None) -> None:
    _ensure_image(RDS_POSTGRES_IMAGE)
    rds = floci_client("rds")
    instance = unique_name("acca-rds")
    user, password = "acca_test", "test-only-pw"
    rds.create_db_instance(
        DBInstanceIdentifier=instance,
        Engine="postgres",
        DBInstanceClass="db.t3.micro",
        MasterUsername=user,
        MasterUserPassword=password,
        AllocatedStorage=20,
    )
    resource_id: str | None = None
    deleted = False
    try:

        def available() -> dict[str, Any] | None:
            db: dict[str, Any] = rds.describe_db_instances(DBInstanceIdentifier=instance)[
                "DBInstances"
            ][0]
            return db if db["DBInstanceStatus"] == "available" else None

        db = wait_until(available, timeout=180, interval=2.0)
        resource_id = str(db["DbiResourceId"])
        port = int(db["Endpoint"]["Port"])
        assert port in RDS_PORTS
        assert _rds_volumes(resource_id), "expected the instance's data volume"

        with (
            psycopg.connect(
                host="127.0.0.1",
                port=port,
                user=user,
                password=password,
                dbname="postgres",
                connect_timeout=5,
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("CREATE TABLE acca_probe (id int PRIMARY KEY, note text NOT NULL)")
            cur.execute("INSERT INTO acca_probe VALUES (1, 'floci')")
            cur.execute("SELECT note FROM acca_probe WHERE id = 1")
            assert cur.fetchone() == ("floci",)

        rds.delete_db_instance(DBInstanceIdentifier=instance, SkipFinalSnapshot=True)
        deleted = True

        def gone() -> bool:
            try:
                rds.describe_db_instances(DBInstanceIdentifier=instance)
            except ClientError as exc:
                return _client_error_code(exc) == "DBInstanceNotFound"
            return False

        wait_until(gone, timeout=120, interval=2.0)
        wait_until(lambda: not _rds_volumes(str(resource_id)), timeout=60, interval=1.0)
    finally:
        if not deleted:
            rds.delete_db_instance(DBInstanceIdentifier=instance, SkipFinalSnapshot=True)


# --- Lambda + API Gateway --------------------------------------------------------------------------

HANDLER = """
import json


def handler(event, context):
    path = event.get("rawPath") or event.get("path") or ""
    return {"statusCode": 200, "body": json.dumps({"ok": True, "path": path})}
"""


def _zip_handler() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("app.py", HANDLER)
    return buffer.getvalue()


@pytest.fixture
def python_function(floci: str, docker_ready: None) -> Iterator[tuple[str, str]]:
    """A python3.12 function, Active; yields (name, arn) and deletes it afterwards."""
    _ensure_image(LAMBDA_PYTHON_IMAGE)
    lam = floci_client("lambda")
    name = unique_name("acca-fn")
    arn = lam.create_function(
        FunctionName=name,
        Runtime="python3.12",
        Role=LAMBDA_ROLE,
        Handler="app.handler",
        Code={"ZipFile": _zip_handler()},
        Timeout=10,
    )["FunctionArn"]
    try:
        wait_until(
            lambda: lam.get_function(FunctionName=name)["Configuration"]["State"] == "Active",
            timeout=60,
            interval=1.0,
        )
        yield name, str(arn)
    finally:
        lam.delete_function(FunctionName=name)


def _assert_hello(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "path": "/hello"}


def test_lambda_invoke_and_api_gateway_http_and_rest(python_function: tuple[str, str]) -> None:
    name, function_arn = python_function
    lam = floci_client("lambda")
    invoked = lam.invoke(FunctionName=name, Payload=json.dumps({"rawPath": "/direct"}).encode())
    assert invoked["StatusCode"] == 200
    assert "FunctionError" not in invoked
    payload = json.loads(invoked["Payload"].read())
    assert payload["statusCode"] == 200
    assert json.loads(payload["body"]) == {"ok": True, "path": "/direct"}

    v2 = floci_client("apigatewayv2")
    http_api = v2.create_api(Name=unique_name("acca-http"), ProtocolType="HTTP")["ApiId"]
    rest = floci_client("apigateway")
    rest_api: str | None = None
    try:
        integration = v2.create_integration(
            ApiId=http_api,
            IntegrationType="AWS_PROXY",
            IntegrationUri=function_arn,
            PayloadFormatVersion="2.0",
        )["IntegrationId"]
        v2.create_route(ApiId=http_api, RouteKey="GET /hello", Target=f"integrations/{integration}")
        v2.create_stage(ApiId=http_api, StageName="$default", AutoDeploy=True)
        lam.add_permission(
            FunctionName=name,
            StatementId="acca-http-api",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
        )
        port = FLOCI_ENDPOINT.rsplit(":", 1)[1]
        _assert_hello(
            httpx.get(
                f"{FLOCI_ENDPOINT}/hello",
                # Floci's documented execute-api host; sent as a header so no DNS is needed.
                headers={"Host": f"{http_api}.execute-api.localhost.floci.io:{port}"},
                timeout=30.0,
            )
        )

        rest_api = str(rest.create_rest_api(name=unique_name("acca-rest"))["id"])
        root = next(
            item["id"]
            for item in rest.get_resources(restApiId=rest_api)["items"]
            if item["path"] == "/"
        )
        resource = rest.create_resource(restApiId=rest_api, parentId=root, pathPart="hello")["id"]
        rest.put_method(
            restApiId=rest_api, resourceId=resource, httpMethod="GET", authorizationType="NONE"
        )
        rest.put_integration(
            restApiId=rest_api,
            resourceId=resource,
            httpMethod="GET",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=(
                "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/"
                f"{function_arn}/invocations"
            ),
        )
        rest.create_deployment(restApiId=rest_api, stageName="dev")
        _assert_hello(
            httpx.get(
                f"{FLOCI_ENDPOINT}/restapis/{rest_api}/dev/_user_request_/hello", timeout=30.0
            )
        )
    finally:
        try:
            if rest_api is not None:
                rest.delete_rest_api(restApiId=rest_api)
        finally:
            v2.delete_api(ApiId=http_api)
