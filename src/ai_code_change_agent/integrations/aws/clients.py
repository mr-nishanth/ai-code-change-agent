"""The one place the application builds boto3 clients for DynamoDB, SQS and S3.

One `boto3.session.Session` holds the three clients. Each is created on first use, under a lock
(boto3 clients are thread-safe once created; sessions are not), so a process only builds the
clients it touches: the API, which never calls SQS or S3, has a DynamoDB client only.

With an endpoint override (development/test, the local Floci emulator; Settings guarantees it is a
loopback or single-label Docker host) the session is built from a botocore session whose profile,
config file and shared-credentials file are disabled and the static credentials `test`/`test`: the
default credential chain, `AWS_PROFILE` and the AWS config files are never consulted. Without an
override (staging/production) the process's default AWS credential chain applies. In both cases
`ignore_configured_endpoint_urls=True` makes botocore ignore `AWS_ENDPOINT_URL*` and endpoint
settings in config files, so only the configured endpoint (or real AWS) is ever contacted."""

import os
import threading
from functools import cached_property
from typing import Any

import boto3  # type: ignore[import-untyped]
import botocore.session  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from ai_code_change_agent.core.config import Settings

CONNECT_TIMEOUT_SECONDS = 5
# Above the SQS long-poll wait (ReceiveMessageWaitTimeSeconds=20) so a receive never times out.
READ_TIMEOUT_SECONDS = 60
LOCAL_ACCESS_KEY_ID = "test"
LOCAL_SECRET_ACCESS_KEY = "test"  # noqa: S105 - Floci's documented dummy credential

# botocore session variables (name -> (config key, env var, default, converter)) that would read the
# developer's profile and AWS files; disabled for the local emulator.
_NO_AMBIENT_FILES = {
    "profile": (None, None, None, None),
    "config_file": (None, None, os.devnull, None),
    "credentials_file": (None, None, os.devnull, None),
}


def client_config() -> Config:
    return Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )


def probe_config(timeout_seconds: float) -> Config:
    """A readiness probe: one attempt, bounded by the probe's own timeout (no botocore retries)."""
    return Config(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"mode": "standard", "total_max_attempts": 1},
        ignore_configured_endpoint_urls=True,
    )


class AwsClients:
    """DynamoDB, SQS and S3 clients for one Settings; `queue_url()` resolves the job queue once."""

    def __init__(self, settings: Settings) -> None:
        self._queue_name = settings.sqs_job_queue_name
        self.endpoint_url: str | None = settings.aws_endpoint_url
        self.region: str = settings.aws_region
        if self.endpoint_url is not None:
            self.session = boto3.session.Session(
                aws_access_key_id=LOCAL_ACCESS_KEY_ID,
                aws_secret_access_key=LOCAL_SECRET_ACCESS_KEY,
                region_name=self.region,
                botocore_session=botocore.session.Session(session_vars=_NO_AMBIENT_FILES),
            )
        else:
            self.session = boto3.session.Session(region_name=self.region)
        self._queue_url: str | None = None
        self._lock = threading.Lock()
        self._session_lock = threading.Lock()

    @cached_property
    def dynamodb(self) -> Any:
        return self._client("dynamodb", client_config())

    @cached_property
    def sqs(self) -> Any:
        return self._client("sqs", client_config())

    @cached_property
    def s3(self) -> Any:
        return self._client("s3", client_config())

    def dynamodb_probe(self, timeout_seconds: float) -> Any:
        """A separate DynamoDB client for readiness probes (`probe_config`): a probe never waits
        on the application client's retries and timeouts."""
        return self._client("dynamodb", probe_config(timeout_seconds))

    def _client(self, service: str, config: Config) -> Any:
        with self._session_lock:
            return self.session.client(
                service, region_name=self.region, endpoint_url=self.endpoint_url, config=config
            )

    def queue_url(self) -> str:
        """The job queue URL from GetQueueUrl, looked up on first use and cached."""
        with self._lock:
            if self._queue_url is None:
                response = self.sqs.get_queue_url(QueueName=self._queue_name)
                self._queue_url = str(response["QueueUrl"])
            return self._queue_url
