"""Durable local-filesystem artifact store (single host, development).

Layout: <root>/<request-uuid>/<attempt-uuid>/<artifact-id>.blob and .meta.json
Security properties:
- the root must be an absolute path with no symlink component, owned by this user, mode 0700;
- every path component is built from a UUID or a validated opaque id, never from caller text;
- directories are created 0700, files 0600 with O_EXCL|O_NOFOLLOW and then made read-only (0400);
- an artifact is written once; nothing here updates or deletes;
- every read re-hashes the bytes and compares size and hash with the stored metadata.

The metadata file is not a root of trust (someone able to rewrite the blob can rewrite it too):
callers compare `meta.sha256` with the hash recorded in the database (see load_verified).
Blocking file I/O runs in a worker thread.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
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

MetaList = list[ArtifactMeta]  # the class defines a method named `list`

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)  # a planted FIFO must not hang the host


def ensure_private_root(root: Path) -> Path:
    """Create (if absent) and validate a private storage root. Shared with the workspace runtime."""
    if not root.is_absolute() or ".." in root.parts:
        raise ArtifactStorageError("storage_root_not_absolute")
    if Path(os.path.realpath(root)) != Path(os.path.abspath(root)):
        raise ArtifactStorageError("storage_root_has_symlink")
    if not root.exists():
        if not root.parent.is_dir():
            raise ArtifactStorageError("storage_root_parent_missing")
        root.mkdir(mode=_DIR_MODE)
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ArtifactStorageError("storage_root_not_owned_directory")
    if st.st_mode & 0o077:
        raise ArtifactStorageError("storage_root_permissions_too_open")
    return root


def _private_dir(path: Path) -> None:
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=_DIR_MODE)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ArtifactStorageError("storage_directory_unsafe")


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, _FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o400)


def _read_file(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError from exc
    except OSError as exc:  # includes ELOOP for a symlink
        raise ArtifactStorageError("artifact_unreadable") from exc
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ArtifactStorageError("artifact_not_a_regular_file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ArtifactTooLargeError
    return data


class LocalFilesystemArtifactStore:
    def __init__(self, root: Path, *, max_artifact_bytes: int = 5 * 1024 * 1024) -> None:
        self._root = ensure_private_root(root)
        self._max = max_artifact_bytes

    # ----- paths: built only from UUIDs and validated ids ---------------------------------
    def _scope_dir(self, scope: ArtifactScope, *, create: bool) -> Path:
        request = self._root / str(uuid.UUID(str(scope.request_id)))
        attempt = request / str(uuid.UUID(str(scope.attempt_id)))
        if create:
            _private_dir(request)
            _private_dir(attempt)
        elif not attempt.is_dir() or attempt.is_symlink() or request.is_symlink():
            raise ArtifactNotFoundError
        return attempt

    @staticmethod
    def _check_id(artifact_id: str) -> None:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            raise ArtifactNotFoundError  # a malformed id cannot name an artifact

    # ----- port ---------------------------------------------------------------------------
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
        return await asyncio.to_thread(
            self._put, scope, kind, content, redactions, redaction_version
        )

    def _put(
        self,
        scope: ArtifactScope,
        kind: ArtifactKind,
        content: bytes,
        redactions: int,
        version: str,
    ) -> ArtifactMeta:
        directory = self._scope_dir(scope, create=True)
        meta = ArtifactMeta(
            id=f"art_{uuid.uuid4().hex}",
            kind=kind,
            scope=scope,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            created_at=datetime.now(UTC),
            redaction_version=version,
            redactions=redactions,
        )
        _write_new(directory / f"{meta.id}.blob", content)
        _write_new(directory / f"{meta.id}.meta.json", encode_meta(meta))
        return meta

    async def get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        return await asyncio.to_thread(self._get, scope, artifact_id)

    def _get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        meta = self._meta(scope, artifact_id)
        data = _read_file(self._scope_dir(scope, create=False) / f"{artifact_id}.blob", self._max)
        if len(data) != meta.size or hashlib.sha256(data).hexdigest() != meta.sha256:
            raise ArtifactIntegrityError
        return data

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

    def _list(self, scope: ArtifactScope) -> MetaList:
        try:
            directory = self._scope_dir(scope, create=False)
        except ArtifactNotFoundError:
            return []
        out = [
            self._meta(scope, name.removesuffix(".meta.json"))
            for name in sorted(os.listdir(directory))
            if name.endswith(".meta.json")
            and ARTIFACT_ID.fullmatch(name.removesuffix(".meta.json"))
        ]
        return sorted(out, key=lambda m: (m.created_at, m.id))

    # ----- metadata -----------------------------------------------------------------------
    def _meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        self._check_id(artifact_id)
        raw = _read_file(
            self._scope_dir(scope, create=False) / f"{artifact_id}.meta.json", META_MAX_BYTES
        )
        return decode_meta(raw, scope, artifact_id)


# ----- metadata document (shared with the S3 store) ------------------------------------------
META_MAX_BYTES = 65_536


def encode_meta(meta: ArtifactMeta) -> bytes:
    return json.dumps(
        {
            "id": meta.id,
            "kind": meta.kind.value,
            "request_id": str(meta.scope.request_id),
            "attempt_id": str(meta.scope.attempt_id),
            "sha256": meta.sha256,
            "size": meta.size,
            "created_at": meta.created_at.isoformat(),
            "redaction_version": meta.redaction_version,
            "redactions": meta.redactions,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def decode_meta(raw: bytes, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
    """Parse a metadata document; it must name exactly this id and scope (else: not found)."""
    try:
        doc: dict[str, Any] = json.loads(raw)
        meta = ArtifactMeta(
            id=str(doc["id"]),
            kind=ArtifactKind(doc["kind"]),
            scope=ArtifactScope(uuid.UUID(doc["request_id"]), uuid.UUID(doc["attempt_id"])),
            sha256=str(doc["sha256"]),
            size=int(doc["size"]),
            created_at=datetime.fromisoformat(doc["created_at"]),
            redaction_version=str(doc["redaction_version"]),
            redactions=int(doc["redactions"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ArtifactIntegrityError("artifact_metadata_invalid") from exc
    if meta.id != artifact_id or meta.scope != scope:  # a copied or edited document
        raise ArtifactNotFoundError
    return meta
