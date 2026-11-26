"""Deterministic artifact serialisation and hash-verified reads."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from ai_code_change_agent.application.artifacts import errors as artifact_errors
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.artifacts.ports import ScopedArtifacts
from ai_code_change_agent.application.errors import ArtifactIntegrityError


@dataclass(frozen=True)
class StoredArtifact:
    ref: str
    sha256: str


def canonical_json(model: BaseModel) -> bytes:
    """Same model value, same bytes: sorted keys, no whitespace, ASCII only."""
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def store_model(
    store: ScopedArtifacts, scope: ArtifactScope, kind: ArtifactKind, model: BaseModel
) -> StoredArtifact:
    content = canonical_json(model)
    meta = await store.put(scope, kind, content)
    if meta.sha256 != sha256_hex(content) and meta.redactions == 0:
        raise ArtifactIntegrityError("artifact_store_hash_mismatch")  # the store altered the bytes
    return StoredArtifact(meta.id, meta.sha256)


async def load_verified[M: BaseModel](
    store: ScopedArtifacts,
    scope: ArtifactScope,
    ref: str,
    expected_sha256: str,
    model: type[M],
) -> M:
    """A reference alone proves nothing: re-hash the bytes and compare with the recorded hash."""
    try:
        content = await store.get(scope, ref)
    except artifact_errors.ArtifactNotFoundError as exc:
        raise ArtifactIntegrityError("artifact_missing") from exc
    except artifact_errors.ArtifactError as exc:
        raise ArtifactIntegrityError("artifact_hash_mismatch") from exc
    if sha256_hex(content) != expected_sha256:
        raise ArtifactIntegrityError("artifact_hash_mismatch")
    try:
        return model.model_validate_json(content)
    except ValidationError as exc:
        raise ArtifactIntegrityError("artifact_schema_invalid") from exc
