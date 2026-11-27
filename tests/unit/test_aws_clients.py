"""The AWS client factory (integrations/aws/clients.py): endpoint, region, credentials, isolation.

No request leaves the process: a before-send hook answers in place of the network and records
where botocore would have sent the request."""

from collections.abc import Iterator
from typing import Any

import pytest
from botocore.awsrequest import AWSResponse  # type: ignore[import-untyped]
from botocore.stub import Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.integrations.aws.clients import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    client_config,
)

LOCAL = "http://127.0.0.1:4566"
PROD = {
    "app_env": "production",
    "llm_provider": "bedrock",
    "jira_webhook_secret": "x",
    "jira_project_keys": "SHOP",
}


def local_settings(**kw: object) -> Settings:
    return Settings(_env_file=None, app_env="test", **kw)  # type: ignore[arg-type]


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_: object) -> Iterator[bytes]:
        yield self._body


def capture(client: Any, body: bytes = b'{"TableNames": []}') -> list[Any]:
    """Answer every request locally; return the list the sent requests are appended to."""
    sent: list[Any] = []

    def before_send(request: Any, **_: object) -> AWSResponse:
        sent.append(request)
        return AWSResponse(request.url, 200, {}, _Raw(body))

    client.meta.events.register("before-send", before_send)
    return sent


def test_local_clients_use_the_endpoint_region_and_static_credentials() -> None:
    aws = AwsClients(local_settings(ACCA_AWS_REGION="eu-west-1"))
    for client in (aws.dynamodb, aws.sqs, aws.s3):
        assert client.meta.endpoint_url == LOCAL
        assert client.meta.region_name == "eu-west-1"
        config = client.meta.config
        assert (config.connect_timeout, config.read_timeout) == (
            CONNECT_TIMEOUT_SECONDS,
            READ_TIMEOUT_SECONDS,
        )
        assert config.retries == {"mode": "standard"}
    # consumed at client creation (not kept on client.meta.config); its effect is proven below
    assert client_config().ignore_configured_endpoint_urls is True
    credentials = aws.session.get_credentials()
    assert (credentials.access_key, credentials.secret_key) == ("test", "test")
    assert {
        aws.dynamodb.meta.service_model.service_name,
        aws.sqs.meta.service_model.service_name,
    } == {
        "dynamodb",
        "sqs",
    }


def test_ambient_aws_profile_and_endpoint_are_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "http://203.0.113.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAREALLOOKINGKEY00")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real-looking-secret")
    config = tmp_path / "config"
    config.write_text("[default]\nendpoint_url = http://203.0.113.1:1\nregion = ap-south-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    aws = AwsClients(local_settings())  # no ProfileNotFound
    sent = capture(aws.dynamodb)
    assert aws.dynamodb.list_tables()["TableNames"] == []
    assert sent[0].url == LOCAL + "/"
    assert "Credential=test/" in sent[0].headers["Authorization"].decode()
    assert aws.session.get_credentials().access_key == "test"


def test_deployed_clients_use_real_aws_and_ignore_ambient_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    aws = AwsClients(Settings(_env_file=None, **PROD))  # type: ignore[arg-type]
    assert aws.endpoint_url is None
    assert aws.dynamodb.meta.endpoint_url == "https://dynamodb.us-east-1.amazonaws.com"
    assert aws.sqs.meta.endpoint_url == "https://sqs.us-east-1.amazonaws.com"


def test_queue_url_is_looked_up_once_by_name() -> None:
    aws = AwsClients(local_settings(ACCA_SQS_JOB_QUEUE_NAME="s2116-jobs"))
    url = "http://127.0.0.1:4566/000000000000/s2116-jobs"
    with Stubber(aws.sqs) as stub:
        stub.add_response("get_queue_url", {"QueueUrl": url}, {"QueueName": "s2116-jobs"})
        assert aws.queue_url() == url
        assert aws.queue_url() == url  # cached: a second GetQueueUrl would fail the stubber
        stub.assert_no_pending_responses()
