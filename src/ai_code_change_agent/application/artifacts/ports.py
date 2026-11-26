"""Artifact storage port. Adapters: `integrations/artifacts` (local filesystem today, S3 later)."""

from typing import Protocol

from ai_code_change_agent.application.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactScope,
)

MetaList = list[ArtifactMeta]  # protocols below define a method named `list`


class ArtifactStore(Protocol):
    async def put(
        self,
        scope: ArtifactScope,
        kind: ArtifactKind,
        content: bytes,
        *,
        redactions: int = 0,
        redaction_version: str = "",
    ) -> ArtifactMeta:
        """Store immutable bytes under a new opaque id. Never overwrites."""
        ...

    async def get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        """Return the bytes after re-hashing them. Raises not-found or integrity errors."""
        ...

    async def meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta: ...

    async def exists(self, scope: ArtifactScope, artifact_id: str) -> bool: ...

    async def verify(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        """Re-hash the stored bytes and return the metadata, or raise."""
        ...

    async def list(self, scope: ArtifactScope) -> MetaList: ...


class ScopedArtifacts(Protocol):
    """What application code uses: the redacting `ArtifactService` (or a test fake)."""

    async def put(
        self, scope: ArtifactScope, kind: ArtifactKind, content: bytes
    ) -> ArtifactMeta: ...

    async def get(self, scope: ArtifactScope, artifact_id: str) -> bytes: ...

    async def meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta: ...

    async def exists(self, scope: ArtifactScope, artifact_id: str) -> bool: ...

    async def verify(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta: ...

    async def list(self, scope: ArtifactScope) -> MetaList: ...
