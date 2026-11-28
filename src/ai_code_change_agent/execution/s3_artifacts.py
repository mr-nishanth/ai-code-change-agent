"""S3 artifact store (DYNAMODB-DATA-MODEL.md section 12). Not wired into composition yet.

Layout (one bucket, shared with the checkpoint offload under its own prefix):
`artifacts/<request-uuid>/<attempt-uuid>/<artifact-id>.blob` and `.meta.json`.
Security properties, matching the local filesystem store:
- every key is built from UUIDs and a validated opaque id, never from caller text;
- an object is written once: `PutObject` with `IfNoneMatch="*"`, so an existing key is never
  replaced (S3 answers `PreconditionFailed`; verified on Floci 2.5.0, DATA-MODEL 10.2);
  nothing here updates or deletes;
- every read re-hashes the bytes and compares size and hash with the stored metadata; reads are
  bounded (an object larger than the limit is refused before its body is read);
- S3 and botocore failures (classified in `integrations/aws/s3.py`, the only place that sees
  botocore errors) become `ArtifactStorageError("artifact_storage_unavailable")`, not chained, so
  neither the service message nor bucket, key or request id travels with it; the AWS error code (or
  the botocore exception name) is kept on `aws_code` for logs.

The metadata object is not a root of trust (anyone able to rewrite the blob can rewrite it too):
callers compare `meta.sha256` with the hash recorded in the database.
boto3 calls are blocking and run in a worker thread (boto3 clients are thread-safe).
"""

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import (
    ARTIFACT_ID,
    ArtifactKind,
    ArtifactMeta,
    ArtifactScope,
)
from ai_code_change_agent.execution.artifacts import META_MAX_BYTES, decode_meta, encode_meta
from ai_code_change_agent.integrations.aws.s3 import (
    S3ObjectExists,
    S3ObjectMissing,
    S3ObjectTooLarge,
    S3Unavailable,
    list_keys,
    put_if_absent,
    read_bounded,
)

MetaList = list[ArtifactMeta]  # the class defines a method named `list`

ARTIFACT_S3_PREFIX = "artifacts/"
BLOB_SUFFIX = ".blob"
META_SUFFIX = ".meta.json"
STORAGE_UNAVAILABLE = "artifact_storage_unavailable"
ALREADY_EXISTS = "artifact_already_exists"


class S3StorageError(ArtifactStorageError):
    """An S3 or transport failure. The message is fixed; `aws_code` names the error code only."""

    def __init__(self, aws_code: str) -> None:
        super().__init__(STORAGE_UNAVAILABLE)
        self.aws_code = aws_code


def _mapped[T](call: Callable[[], T]) -> T:
    """Run one S3 operation and turn its outcome errors into artifact errors (not chained)."""
    try:
        return call()
    except S3ObjectExists:
        raise ArtifactIntegrityError(ALREADY_EXISTS) from None
    except S3ObjectMissing:
        raise ArtifactNotFoundError from None
    except S3ObjectTooLarge:
        raise ArtifactTooLargeError from None
    except S3Unavailable as exc:
        raise S3StorageError(exc.aws_code) from None


class S3ArtifactStore:
    LIST_PAGE_SIZE = 1000

    def __init__(self, s3: Any, bucket: str, *, max_artifact_bytes: int = 5 * 1024 * 1024) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._max = max_artifact_bytes

    # ----- keys: built only from UUIDs and validated ids -----------------------------------
    @staticmethod
    def _prefix(scope: ArtifactScope) -> str:
        request = uuid.UUID(str(scope.request_id))
        attempt = uuid.UUID(str(scope.attempt_id))
        return f"{ARTIFACT_S3_PREFIX}{request}/{attempt}/"

    @staticmethod
    def _check_id(artifact_id: str) -> None:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            raise ArtifactNotFoundError  # a malformed id cannot name an object

    @staticmethod
    def _new_id() -> str:
        return f"art_{uuid.uuid4().hex}"

    # ----- port -----------------------------------------------------------------------------
    async def put(
        self,
        scope: ArtifactScope,
        kind: ArtifactKind,
        content: bytes,
        *,
        redactions: int = 0,
        redaction_version: str = "",
    ) -> ArtifactMeta:
        if len(content) > self._max:
            raise ArtifactTooLargeError
        prefix = self._prefix(scope)
        meta = ArtifactMeta(
            id=self._new_id(),
            kind=kind,
            scope=scope,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            created_at=datetime.now(UTC),
            redaction_version=redaction_version,
            redactions=redactions,
        )
        # The blob first: the metadata object is what makes an artifact visible.
        await asyncio.to_thread(self._put_new, f"{prefix}{meta.id}{BLOB_SUFFIX}", content)
        await asyncio.to_thread(self._put_new, f"{prefix}{meta.id}{META_SUFFIX}", encode_meta(meta))
        return meta

    async def get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        return await asyncio.to_thread(self._get, scope, artifact_id)

    async def meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        return await asyncio.to_thread(self._meta, scope, artifact_id)

    async def exists(self, scope: ArtifactScope, artifact_id: str) -> bool:
        try:
            await self.meta(scope, artifact_id)
        except ArtifactNotFoundError:
            return False
        return True

    async def verify(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        await self.get(scope, artifact_id)
        return await self.meta(scope, artifact_id)

    async def list(self, scope: ArtifactScope) -> MetaList:
        return await asyncio.to_thread(self._list, scope)

    # ----- blocking S3 calls (worker thread) ------------------------------------------------
    def _put_new(self, key: str, body: bytes) -> None:
        _mapped(lambda: put_if_absent(self._s3, self._bucket, key, body))

    def _read(self, key: str, limit: int) -> bytes:
        return _mapped(lambda: read_bounded(self._s3, self._bucket, key, limit))

    def _meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        self._check_id(artifact_id)
        raw = self._read(f"{self._prefix(scope)}{artifact_id}{META_SUFFIX}", META_MAX_BYTES)
        return decode_meta(raw, scope, artifact_id)

    def _get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        meta = self._meta(scope, artifact_id)
        data = self._read(f"{self._prefix(scope)}{artifact_id}{BLOB_SUFFIX}", self._max)
        if len(data) != meta.size or hashlib.sha256(data).hexdigest() != meta.sha256:
            raise ArtifactIntegrityError
        return data

    def _list(self, scope: ArtifactScope) -> MetaList:
        prefix = self._prefix(scope)
        keys = _mapped(
            lambda: list_keys(self._s3, self._bucket, prefix, page_size=self.LIST_PAGE_SIZE)
        )
        names = (key.removeprefix(prefix).removesuffix(META_SUFFIX) for key in keys)
        ids = sorted(
            name
            for key, name in zip(keys, names, strict=True)
            if key.endswith(META_SUFFIX) and ARTIFACT_ID.fullmatch(name)
        )
        out = [self._meta(scope, artifact_id) for artifact_id in ids]
        return sorted(out, key=lambda m: (m.created_at, m.id))
