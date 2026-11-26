"""The local-filesystem artifact store: integrity, scoping, immutability and path safety."""

import asyncio
import os
import stat
import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.execution.artifacts import (
    LocalFilesystemArtifactStore,
    ensure_private_root,
)

K = ArtifactKind
SCOPE = ArtifactScope(uuid.uuid4(), uuid.uuid4())
OTHER_ATTEMPT = ArtifactScope(SCOPE.request_id, uuid.uuid4())
OTHER_REQUEST = ArtifactScope(uuid.uuid4(), SCOPE.attempt_id)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def store(root: Path) -> LocalFilesystemArtifactStore:
    return LocalFilesystemArtifactStore(root, max_artifact_bytes=1024)


def files_of(root: Path, scope: ArtifactScope = SCOPE) -> Path:
    return root / str(scope.request_id) / str(scope.attempt_id)


async def test_store_and_retrieve_with_metadata(store: LocalFilesystemArtifactStore) -> None:
    meta = await store.put(
        SCOPE, K.IMPLEMENTATION_PLAN, b'{"a":1}', redactions=2, redaction_version="9"
    )
    assert meta.id.startswith("art_") and len(meta.id) == 36 and meta.size == 7
    assert (meta.redactions, meta.redaction_version, meta.kind) == (2, "9", K.IMPLEMENTATION_PLAN)
    assert await store.get(SCOPE, meta.id) == b'{"a":1}'
    assert await store.meta(SCOPE, meta.id) == meta
    assert await store.exists(SCOPE, meta.id)
    assert (await store.verify(SCOPE, meta.id)) == meta
    assert await store.list(SCOPE) == [meta]


async def test_every_put_gets_a_new_opaque_id_and_nothing_is_overwritten(
    store: LocalFilesystemArtifactStore,
) -> None:
    a = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"same")
    b = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"same")
    assert a.id != b.id and a.sha256 == b.sha256
    assert await store.get(SCOPE, a.id) == await store.get(SCOPE, b.id) == b"same"


async def test_stored_files_are_private_and_read_only(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.VALIDATION_REPORT, b"x")
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(files_of(root)).st_mode) == 0o700
    for suffix in (".blob", ".meta.json"):
        mode = stat.S_IMODE(os.stat(files_of(root) / f"{meta.id}{suffix}").st_mode)
        assert mode == 0o400


async def test_tampered_blob_is_detected_on_every_read(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.CHANGESET, b"original")
    blob = files_of(root) / f"{meta.id}.blob"
    blob.chmod(0o600)
    blob.write_bytes(b"tampered")
    for call in (store.get, store.verify):
        with pytest.raises(ArtifactIntegrityError):
            await call(SCOPE, meta.id)


async def test_same_size_tamper_is_detected_by_the_hash(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.CHANGESET, b"original")
    blob = files_of(root) / f"{meta.id}.blob"
    blob.chmod(0o600)
    blob.write_bytes(b"0riginal")  # same length
    with pytest.raises(ArtifactIntegrityError):
        await store.get(SCOPE, meta.id)


async def test_corrupt_metadata_fails_closed(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    path = files_of(root) / f"{meta.id}.meta.json"
    path.chmod(0o600)
    path.write_text("{not json")
    with pytest.raises(ArtifactIntegrityError, match="artifact_metadata_invalid"):
        await store.meta(SCOPE, meta.id)


async def test_artifacts_are_invisible_outside_their_request_and_attempt(
    store: LocalFilesystemArtifactStore,
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"secret-plan")
    for other in (OTHER_ATTEMPT, OTHER_REQUEST):
        with pytest.raises(ArtifactNotFoundError):
            await store.get(other, meta.id)
        with pytest.raises(ArtifactNotFoundError):
            await store.meta(other, meta.id)
        assert not await store.exists(other, meta.id)
        assert await store.list(other) == []


async def test_a_copied_artifact_does_not_authenticate_in_another_scope(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    other = files_of(root, OTHER_ATTEMPT)
    other.parent.mkdir(exist_ok=True, mode=0o700)
    other.mkdir(mode=0o700)
    for suffix in (".blob", ".meta.json"):
        (other / f"{meta.id}{suffix}").write_bytes(
            (files_of(root) / f"{meta.id}{suffix}").read_bytes()
        )
    with pytest.raises(ArtifactNotFoundError):  # metadata names the original scope
        await store.get(OTHER_ATTEMPT, meta.id)


@pytest.mark.parametrize(
    "bad_id",
    [
        "../x",
        "art_../../etc/passwd",
        "art_" + "g" * 32,
        "art_" + "0" * 31,
        "",
        "/etc/passwd",
        "ART_" + "0" * 32,
        "art_" + "0" * 32 + "/..",
    ],
)
async def test_malformed_ids_cannot_name_a_path(
    store: LocalFilesystemArtifactStore, bad_id: str
) -> None:
    await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    for call in (store.get, store.meta, store.verify):
        with pytest.raises(ArtifactNotFoundError):
            await call(SCOPE, bad_id)
    assert not await store.exists(SCOPE, bad_id)


async def test_a_symlinked_blob_is_refused_not_followed(
    store: LocalFilesystemArtifactStore, root: Path, tmp_path: Path
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"x")
    blob = files_of(root) / f"{meta.id}.blob"
    blob.chmod(0o600)
    blob.unlink()
    blob.symlink_to(outside)
    with pytest.raises(ArtifactStorageError):
        await store.get(SCOPE, meta.id)


async def test_a_symlinked_scope_directory_is_refused(
    store: LocalFilesystemArtifactStore, root: Path, tmp_path: Path
) -> None:
    await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    target = tmp_path / "elsewhere"
    target.mkdir()
    victim = ArtifactScope(uuid.uuid4(), uuid.uuid4())
    (root / str(victim.request_id)).symlink_to(target)
    with pytest.raises(ArtifactStorageError):
        await store.put(victim, K.IMPLEMENTATION_PLAN, b"x")
    assert list(target.iterdir()) == []  # nothing was written through the link


async def test_size_limit_is_enforced_on_write(store: LocalFilesystemArtifactStore) -> None:
    with pytest.raises(ArtifactTooLargeError):
        await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x" * 1025)
    assert await store.list(SCOPE) == []


async def test_list_is_ordered_and_ignores_unrelated_files(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    a = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"a")
    b = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"b")
    (files_of(root) / "notes.txt").write_text("ignored")
    assert [m.id for m in await store.list(SCOPE)] == [a.id, b.id]


async def test_concurrent_puts_get_distinct_ids(store: LocalFilesystemArtifactStore) -> None:
    metas = await asyncio.gather(*(store.put(SCOPE, K.VALIDATION_EVIDENCE, b"e") for _ in range(8)))
    assert len({m.id for m in metas}) == 8


async def test_blob_without_metadata_is_an_unreferenced_orphan(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    (files_of(root) / f"{meta.id}.meta.json").chmod(0o600)
    (files_of(root) / f"{meta.id}.meta.json").unlink()
    with pytest.raises(ArtifactNotFoundError):
        await store.get(SCOPE, meta.id)
    assert await store.list(SCOPE) == []


# ---- root validation
def test_root_must_be_absolute_and_private(tmp_path: Path) -> None:
    with pytest.raises(ArtifactStorageError, match="not_absolute"):
        ensure_private_root(Path("relative/dir"))
    with pytest.raises(ArtifactStorageError, match="not_absolute"):
        ensure_private_root(tmp_path / ".." / "x")
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(ArtifactStorageError, match="permissions_too_open"):
        ensure_private_root(open_dir)


def test_root_with_a_symlink_component_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ArtifactStorageError, match="has_symlink"):
        ensure_private_root(link / "store")


def test_root_creation_needs_an_existing_parent_and_is_created_private(tmp_path: Path) -> None:
    with pytest.raises(ArtifactStorageError, match="parent_missing"):
        ensure_private_root(tmp_path / "missing" / "store")
    created = ensure_private_root(tmp_path / "store")
    assert stat.S_IMODE(os.stat(created).st_mode) == 0o700
    assert ensure_private_root(created) == created  # idempotent


def test_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    f.chmod(0o700)
    with pytest.raises(ArtifactStorageError, match="not_owned_directory"):
        ensure_private_root(f)


async def test_a_fifo_in_place_of_a_blob_is_refused_without_blocking(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
    blob = files_of(root) / f"{meta.id}.blob"
    blob.chmod(0o600)
    blob.unlink()
    os.mkfifo(blob)
    with pytest.raises(ArtifactStorageError):
        await asyncio.wait_for(store.get(SCOPE, meta.id), 5)  # would hang forever if it blocked
