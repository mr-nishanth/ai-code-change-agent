"""S3 artifact store error mapping without a network: botocore `Stubber` for service errors Floci
does not produce on demand, and a client that raises botocore's own transport errors."""

import uuid
from typing import Any

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.stub import Stubber  # type: ignore[import-untyped]

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.execution.s3_artifacts import (
    STORAGE_UNAVAILABLE,
    S3ArtifactStore,
    S3StorageError,
)
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.integrations.aws.s3 import S3Unavailable

SCOPE = ArtifactScope(uuid.uuid4(), uuid.uuid4())
ARTIFACT = f"art_{uuid.uuid4().hex}"
SECRET_DETAIL = "arn:aws:iam::123456789012:user/someone RequestId=ABC123 bucket=private-bucket"


@pytest.fixture
def s3() -> Any:
    settings = Settings(
        _env_file=None, app_env="test", ACCA_AWS_ENDPOINT_URL="http://127.0.0.1:4566"
    )
    return AwsClients(settings).s3


def _assert_fixed(error: BaseException, code: str) -> None:
    assert isinstance(error, S3StorageError) and str(error) == STORAGE_UNAVAILABLE
    assert error.aws_code == code
    assert error.__cause__ is None and error.__suppress_context__
    assert "private-bucket" not in repr(error) and "RequestId" not in repr(error)


@pytest.mark.parametrize("code", ["AccessDenied", "SlowDown", "InternalError"])
async def test_service_errors_become_a_fixed_storage_error(s3: Any, code: str) -> None:
    store = S3ArtifactStore(s3, "private-bucket")
    with Stubber(s3) as stub:
        stub.add_client_error("put_object", service_error_code=code, service_message=SECRET_DETAIL)
        stub.add_client_error("get_object", service_error_code=code, service_message=SECRET_DETAIL)
        stub.add_client_error(
            "list_objects_v2", service_error_code=code, service_message=SECRET_DETAIL
        )
        for call in (
            lambda: store.put(SCOPE, ArtifactKind.IMPLEMENTATION_PLAN, b"x"),
            lambda: store.meta(SCOPE, ARTIFACT),
            lambda: store.list(SCOPE),
        ):
            with pytest.raises(ArtifactStorageError) as caught:
                await call()
            _assert_fixed(caught.value, code)


async def test_a_missing_key_is_not_found_and_a_concurrent_conditional_write_is_refused(
    s3: Any,
) -> None:
    store = S3ArtifactStore(s3, "private-bucket")
    with Stubber(s3) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stub.add_client_error(
            "put_object", service_error_code="ConditionalRequestConflict", http_status_code=409
        )
        with pytest.raises(ArtifactNotFoundError):
            await store.meta(SCOPE, ARTIFACT)
        with pytest.raises(ArtifactIntegrityError, match="artifact_already_exists"):
            await store.put(SCOPE, ArtifactKind.IMPLEMENTATION_PLAN, b"x")


async def test_an_object_reported_larger_than_the_limit_is_not_read(s3: Any) -> None:
    class Body:
        read_called = False
        closed = False

        def read(self, _n: int) -> bytes:
            Body.read_called = True
            return b""

        def close(self) -> None:
            Body.closed = True

    class Client:
        def get_object(self, **_: Any) -> dict[str, Any]:
            return {"Body": Body(), "ContentLength": 10**9}

    store = S3ArtifactStore(Client(), "b", max_artifact_bytes=1024)
    with pytest.raises(ArtifactTooLargeError):
        await store.meta(SCOPE, ARTIFACT)
    assert not Body.read_called and Body.closed


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://private-bucket.example"),
        ReadTimeoutError(endpoint_url="http://private-bucket.example"),
    ],
)
async def test_transport_errors_become_a_fixed_storage_error(error: Exception) -> None:
    class Client:
        def put_object(self, **_: Any) -> None:
            raise error

        def get_object(self, **_: Any) -> None:
            raise error

        def get_paginator(self, _name: str) -> Any:
            raise error

    store = S3ArtifactStore(Client(), "private-bucket")
    for call in (
        lambda: store.put(SCOPE, ArtifactKind.IMPLEMENTATION_PLAN, b"x"),
        lambda: store.get(SCOPE, ARTIFACT),
        lambda: store.list(SCOPE),
    ):
        with pytest.raises(ArtifactStorageError) as caught:
            await call()
        _assert_fixed(caught.value, type(error).__name__)


def test_an_unsafe_error_code_is_not_kept() -> None:
    assert S3Unavailable("Bad Code <script>").aws_code == "unknown"
