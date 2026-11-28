"""Write-once, bounded S3 object operations. botocore errors stay in this module.

Every failure becomes one of the exceptions below. Their messages are fixed codes; the AWS error
code (or the botocore exception name) is kept on `S3Unavailable.aws_code` only. Nothing is chained
(`raise ... from None`), so the service message, bucket, key and request id never travel with them.
All calls are blocking (run them in a worker thread); boto3 clients are thread-safe."""

import re
from typing import Any, NoReturn

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
# 412 when the key exists; 409 when a concurrent conditional write to the same key is in flight.
_EXISTS_CODES = frozenset({"PreconditionFailed", "412", "ConditionalRequestConflict", "409"})
_SAFE_CODE = re.compile(r"[A-Za-z0-9.]{1,64}")


class S3ObjectExists(Exception):  # noqa: N818 - an outcome, not a failure of this module
    def __init__(self) -> None:
        super().__init__("s3_object_exists")


class S3ObjectMissing(Exception):  # noqa: N818
    def __init__(self) -> None:
        super().__init__("s3_object_missing")


class S3ObjectTooLarge(Exception):  # noqa: N818
    def __init__(self) -> None:
        super().__init__("s3_object_too_large")


class S3Unavailable(Exception):  # noqa: N818
    def __init__(self, aws_code: str) -> None:
        super().__init__("s3_unavailable")
        self.aws_code = aws_code if _SAFE_CODE.fullmatch(aws_code) else "unknown"


def _error_code(exc: ClientError) -> str:
    response = exc.response if isinstance(exc.response, dict) else {}
    return str((response.get("Error") or {}).get("Code") or "ClientError")


def _unavailable(exc: Exception) -> NoReturn:
    code = _error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
    raise S3Unavailable(code) from None


def put_if_absent(s3: Any, bucket: str, key: str, body: bytes) -> None:
    """`PutObject` with `IfNoneMatch="*"`: never replaces an existing object."""
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            IfNoneMatch="*",
            ContentType="application/octet-stream",
        )
    except ClientError as exc:
        if _error_code(exc) in _EXISTS_CODES:
            raise S3ObjectExists from None
        _unavailable(exc)
    except BotoCoreError as exc:
        _unavailable(exc)


def read_bounded(s3: Any, bucket: str, key: str, limit: int) -> bytes:
    """The object's bytes; an object over `limit` bytes is refused (by its reported length before
    the body is read, and by the bytes actually received)."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            if int(response.get("ContentLength", 0)) > limit:
                raise S3ObjectTooLarge
            data = bytes(body.read(limit + 1))
        finally:
            body.close()
    except ClientError as exc:
        if _error_code(exc) in _NOT_FOUND_CODES:
            raise S3ObjectMissing from None
        _unavailable(exc)
    except BotoCoreError as exc:
        _unavailable(exc)
    if len(data) > limit:
        raise S3ObjectTooLarge
    return data


def list_keys(s3: Any, bucket: str, prefix: str, *, page_size: int = 1000) -> list[str]:
    """Every key under `prefix`, following `ListObjectsV2` pages."""
    keys: list[str] = []
    try:
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, PaginationConfig={"PageSize": page_size}
        )
        for page in pages:
            keys.extend(str(entry["Key"]) for entry in page.get("Contents", []))
    except (ClientError, BotoCoreError) as exc:
        _unavailable(exc)
    return keys
