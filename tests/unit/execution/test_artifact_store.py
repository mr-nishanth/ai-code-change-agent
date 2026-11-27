"""The local-filesystem artifact store: integrity, scoping, immutability and path safety."""

import asyncio
import os
import stat
import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.artifacts.errors import ArtifactStorageError
from ai_code_change_agent.application.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactScope,
)
from ai_code_change_agent.execution.artifacts import (
    LocalFilesystemArtifactStore,
    ensure_private_root,
)
from tests.support.artifact_contract import LIMIT, ArtifactStoreContract

K = ArtifactKind
SCOPE = ArtifactScope(uuid.uuid4(), uuid.uuid4())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def store(root: Path) -> LocalFilesystemArtifactStore:
    return LocalFilesystemArtifactStore(root, max_artifact_bytes=LIMIT)


def files_of(root: Path, scope: ArtifactScope = SCOPE) -> Path:
    return root / str(scope.request_id) / str(scope.attempt_id)


class FilesystemBackdoor:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _replace(self, path: Path, data: bytes) -> None:
        path.chmod(0o600)
        path.write_bytes(data)

    def replace_blob(self, meta: ArtifactMeta, data: bytes) -> None:
        self._replace(files_of(self._root, meta.scope) / f"{meta.id}.blob", data)

    def replace_meta(self, meta: ArtifactMeta, raw: bytes) -> None:
        self._replace(files_of(self._root, meta.scope) / f"{meta.id}.meta.json", raw)

    def remove_meta(self, meta: ArtifactMeta) -> None:
        path = files_of(self._root, meta.scope) / f"{meta.id}.meta.json"
        path.chmod(0o600)
        path.unlink()

    def copy_to_scope(self, meta: ArtifactMeta, other: ArtifactScope) -> None:
        target = files_of(self._root, other)
        target.parent.mkdir(exist_ok=True, mode=0o700)
        target.mkdir(mode=0o700)
        for suffix in (".blob", ".meta.json"):
            source = files_of(self._root, meta.scope) / f"{meta.id}{suffix}"
            (target / f"{meta.id}{suffix}").write_bytes(source.read_bytes())

    def add_unrelated(self, scope: ArtifactScope) -> None:
        (files_of(self._root, scope) / "notes.txt").write_text("ignored")


@pytest.fixture
def backdoor(root: Path) -> FilesystemBackdoor:
    return FilesystemBackdoor(root)


class TestFilesystemStoreContract(ArtifactStoreContract):
    """The shared adapter behaviour (tests/support/artifact_contract.py) on the local filesystem."""


async def test_stored_files_are_private_and_read_only(
    store: LocalFilesystemArtifactStore, root: Path
) -> None:
    meta = await store.put(SCOPE, K.VALIDATION_REPORT, b"x")
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(files_of(root)).st_mode) == 0o700
    for suffix in (".blob", ".meta.json"):
        mode = stat.S_IMODE(os.stat(files_of(root) / f"{meta.id}{suffix}").st_mode)
        assert mode == 0o400


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
