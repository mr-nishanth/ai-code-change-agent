"""The S3 artifact store (DYNAMODB-DATA-MODEL.md section 12) on the local Floci emulator.

The shared adapter behaviour (tests/support/artifact_contract.py, also run against the filesystem
store) plus what is specific to S3: write-once through `IfNoneMatch="*"`, the per-attempt quota
through `ArtifactService`, paginated listing and error mapping. Every test gets its own bucket under
a unique `s2116-<uuid>` name, emptied and deleted afterwards. Clients come from `AwsClients` with
test Settings (explicit loopback endpoint, static `test`/`test` credentials); `AWS_PROFILE` and
`AWS_ENDPOINT_URL` are set to unusable values to prove they are ignored."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactStorageError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import ArtifactMeta, ArtifactScope
from ai_code_change_agent.application.artifacts.service import ArtifactService
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.execution.s3_artifacts import (
    ARTIFACT_S3_PREFIX,
    STORAGE_UNAVAILABLE,
    S3ArtifactStore,
    S3StorageError,
)
from ai_code_change_agent.integrations.aws import AwsClients
from tests.support.artifact_contract import LIMIT, SCOPE, ArtifactStoreContract, K
from tests.support.floci import unique_name


def _prefix(scope: ArtifactScope) -> str:
    return f"{ARTIFACT_S3_PREFIX}{scope.request_id}/{scope.attempt_id}/"


def _keys(s3: Any, bucket: str) -> list[str]:
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys.extend(str(entry["Key"]) for entry in page.get("Contents", []))
    return keys


@pytest.fixture
def aws(floci: str, monkeypatch: pytest.MonkeyPatch) -> AwsClients:
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    return AwsClients(Settings(_env_file=None, app_env="test", ACCA_AWS_ENDPOINT_URL=floci))


@pytest.fixture
def bucket(aws: AwsClients) -> Iterator[str]:
    name = unique_name("s2116")
    aws.s3.create_bucket(Bucket=name)
    try:
        yield name
    finally:
        for key in _keys(aws.s3, name):
            aws.s3.delete_object(Bucket=name, Key=key)
        aws.s3.delete_bucket(Bucket=name)


@pytest.fixture
def store(aws: AwsClients, bucket: str) -> S3ArtifactStore:
    return S3ArtifactStore(aws.s3, bucket, max_artifact_bytes=LIMIT)


class S3Backdoor:
    """Writes straight to the bucket, without the store's `IfNoneMatch` guard."""

    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3 = s3
        self._bucket = bucket

    def _key(self, meta: ArtifactMeta, suffix: str, scope: ArtifactScope | None = None) -> str:
        return f"{_prefix(scope or meta.scope)}{meta.id}{suffix}"

    def replace_blob(self, meta: ArtifactMeta, data: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._key(meta, ".blob"), Body=data)

    def replace_meta(self, meta: ArtifactMeta, raw: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self._key(meta, ".meta.json"), Body=raw)

    def remove_meta(self, meta: ArtifactMeta) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=self._key(meta, ".meta.json"))

    def copy_to_scope(self, meta: ArtifactMeta, other: ArtifactScope) -> None:
        for suffix in (".blob", ".meta.json"):
            self._s3.copy_object(
                Bucket=self._bucket,
                Key=self._key(meta, suffix, other),
                CopySource={"Bucket": self._bucket, "Key": self._key(meta, suffix)},
            )

    def add_unrelated(self, scope: ArtifactScope) -> None:
        prefix = _prefix(scope)
        self._s3.put_object(Bucket=self._bucket, Key=f"{prefix}notes.txt", Body=b"ignored")
        nested = f"{prefix}nested/art_{uuid.uuid4().hex}.meta.json"  # not directly in the scope
        self._s3.put_object(Bucket=self._bucket, Key=nested, Body=b"{}")
        self._s3.put_object(Bucket=self._bucket, Key=f"{prefix}art_bad.meta.json", Body=b"{}")


@pytest.fixture
def backdoor(aws: AwsClients, bucket: str) -> S3Backdoor:
    return S3Backdoor(aws.s3, bucket)


class TestS3StoreContract(ArtifactStoreContract):
    """The shared adapter behaviour on S3 (Floci)."""


async def test_objects_use_the_documented_layout(
    store: S3ArtifactStore, aws: AwsClients, bucket: str
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"plan")
    assert sorted(_keys(aws.s3, bucket)) == [
        f"artifacts/{SCOPE.request_id}/{SCOPE.attempt_id}/{meta.id}.blob",
        f"artifacts/{SCOPE.request_id}/{SCOPE.attempt_id}/{meta.id}.meta.json",
    ]


async def test_a_second_put_under_the_same_id_is_refused_and_keeps_the_original(
    store: S3ArtifactStore, monkeypatch: pytest.MonkeyPatch, aws: AwsClients, bucket: str
) -> None:
    fixed = f"art_{uuid.uuid4().hex}"
    monkeypatch.setattr(store, "_new_id", lambda: fixed)
    first = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"original")
    with pytest.raises(ArtifactIntegrityError, match="artifact_already_exists"):
        await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"replacement")
    assert await store.get(SCOPE, fixed) == b"original"
    assert await store.meta(SCOPE, fixed) == first
    assert len(_keys(aws.s3, bucket)) == 2


async def test_the_metadata_object_is_write_once_too(
    store: S3ArtifactStore, aws: AwsClients, bucket: str
) -> None:
    """A blob that is missing its metadata (an interrupted put) is not refilled through the store,
    and an existing metadata object refuses a second conditional write."""
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    with pytest.raises(ArtifactIntegrityError, match="artifact_already_exists"):
        store._put_new(f"{_prefix(SCOPE)}{meta.id}.meta.json", b"{}")
    assert await store.meta(SCOPE, meta.id) == meta


async def test_the_per_attempt_quota_applies_through_the_service(store: S3ArtifactStore) -> None:
    service = ArtifactService(store, max_artifact_bytes=LIMIT, max_attempt_bytes=2 * LIMIT)
    scope = ArtifactScope(uuid.uuid4(), uuid.uuid4())
    await service.put(scope, K.VALIDATION_EVIDENCE, b"e" * LIMIT)
    await service.put(scope, K.VALIDATION_EVIDENCE, b"e" * (LIMIT - 1))
    with pytest.raises(ArtifactTooLargeError, match="attempt_artifact_quota_exceeded"):
        await service.put(scope, K.VALIDATION_EVIDENCE, b"ee")
    with pytest.raises(ArtifactTooLargeError):
        await service.put(scope, K.VALIDATION_EVIDENCE, b"e" * (LIMIT + 1))
    other = ArtifactScope(scope.request_id, uuid.uuid4())
    assert (await service.put(other, K.VALIDATION_EVIDENCE, b"e")).size == 1
    assert sum(m.size for m in await store.list(scope)) == 2 * LIMIT - 1


async def test_listing_follows_pages_in_creation_order(
    store: S3ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "LIST_PAGE_SIZE", 2)
    scope = ArtifactScope(uuid.uuid4(), uuid.uuid4())
    made = [await store.put(scope, K.VALIDATION_EVIDENCE, str(i).encode()) for i in range(5)]
    listed = await store.list(scope)
    assert listed == sorted(made, key=lambda m: (m.created_at, m.id))
    assert {m.id for m in listed} == {m.id for m in made}


async def test_a_missing_bucket_maps_to_a_fixed_storage_error(aws: AwsClients) -> None:
    missing = unique_name("s2116-missing")
    store = S3ArtifactStore(aws.s3, missing, max_artifact_bytes=LIMIT)
    for call in (
        lambda: store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x"),
        lambda: store.list(SCOPE),
        lambda: store.get(SCOPE, f"art_{uuid.uuid4().hex}"),
    ):
        with pytest.raises(ArtifactStorageError) as caught:
            await call()
        error = caught.value
        assert isinstance(error, S3StorageError) and str(error) == STORAGE_UNAVAILABLE
        assert error.aws_code == "NoSuchBucket"
        assert error.__cause__ is None and error.__suppress_context__
        assert missing not in repr(error) and "bucket" not in str(error).lower()
