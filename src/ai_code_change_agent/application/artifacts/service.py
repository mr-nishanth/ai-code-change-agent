"""Artifact service: size limits, central redaction and policy in front of any ArtifactStore.

Composition always wires adapters through this service, so redaction cannot be skipped by a caller.
Kinds whose bytes are hashed and then approved or applied (plans, patches, manifests...) are never
altered: a secret there **refuses** the artifact. Reports and evidence are **redacted**, and the
hash that is recorded is the hash of the redacted bytes that were actually stored.
"""

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactRedactionError,
    ArtifactSecretError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactScope,
)
from ai_code_change_agent.application.artifacts.ports import ArtifactStore
from ai_code_change_agent.application.artifacts.redaction import (
    REDACTION_VERSION,
    find_secrets,
    redact_text,
)

MetaList = list[ArtifactMeta]  # the class defines a method named `list`
K = ArtifactKind
REFUSE_ON_SECRET: frozenset[ArtifactKind] = frozenset(
    {
        K.REPOSITORY_SNAPSHOT,
        K.REPOSITORY_DEPENDENCIES,
        K.IMPLEMENTATION_PLAN,
        K.CHANGESET,
        K.DELIVERY_MANIFEST,
        K.CROSS_REPO_CONTRACT,  # handed to other agents: a secret is refused, never redacted
    }
)
REDACT_ON_SECRET: frozenset[ArtifactKind] = frozenset(
    {
        K.IMPLEMENTATION_REPORT,
        K.VALIDATION_REPORT,
        K.VALIDATION_EVIDENCE,
        K.DELIVERY_RESULT,
        K.DELIVERY_PROGRESS,
        K.DEPENDENCY_LOCK,
        K.AUTOMATIC_DECISION,
        K.JIRA_REPORT,
    }
)


def prepare_for_storage(kind: ArtifactKind, content: bytes) -> tuple[bytes, int]:
    """The central policy: refuse, redact or reject. Used by the service and by test doubles, so a
    fake can never be more permissive than production.

    Fail closed: content that cannot be scanned as text, a scanner failure, or a kind without a
    policy is refused.
    """
    try:
        text = content.decode("utf-8")
        if kind in REFUSE_ON_SECRET:
            if find_secrets(text):
                raise ArtifactSecretError
            return content, 0
        if kind in REDACT_ON_SECRET:
            cleaned, count = redact_text(text)
            return cleaned.encode("utf-8"), count
    except ArtifactSecretError:
        raise
    except Exception as exc:
        raise ArtifactRedactionError from exc
    raise ArtifactRedactionError


class ArtifactService:
    def __init__(
        self,
        store: ArtifactStore,
        *,
        max_artifact_bytes: int = 5 * 1024 * 1024,
        max_attempt_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._store = store
        self._max = max_artifact_bytes
        self._max_attempt = max_attempt_bytes

    async def put(self, scope: ArtifactScope, kind: ArtifactKind, content: bytes) -> ArtifactMeta:
        if len(content) > self._max:
            raise ArtifactTooLargeError
        prepared, redactions = prepare_for_storage(kind, content)
        used = sum(m.size for m in await self._store.list(scope))
        if used + len(prepared) > self._max_attempt:
            raise ArtifactTooLargeError("attempt_artifact_quota_exceeded")
        return await self._store.put(
            scope, kind, prepared, redactions=redactions, redaction_version=REDACTION_VERSION
        )

    async def get(self, scope: ArtifactScope, artifact_id: str) -> bytes:
        return await self._store.get(scope, artifact_id)

    async def meta(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        return await self._store.meta(scope, artifact_id)

    async def exists(self, scope: ArtifactScope, artifact_id: str) -> bool:
        return await self._store.exists(scope, artifact_id)

    async def verify(self, scope: ArtifactScope, artifact_id: str) -> ArtifactMeta:
        return await self._store.verify(scope, artifact_id)

    async def list(self, scope: ArtifactScope) -> MetaList:
        return await self._store.list(scope)
