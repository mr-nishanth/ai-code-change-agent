"""Behaviour every `ArtifactStore` adapter shares: integrity, scoping, immutability, limits, listing.

A test module subclasses `ArtifactStoreContract` (as `Test...`) and provides two fixtures: `store`
(the adapter under test, built with `max_artifact_bytes=LIMIT`) and `backdoor` (an `ArtifactBackdoor`
that changes stored data behind the adapter's back, the way an attacker or a corrupt disk would)."""

import asyncio
import uuid
from typing import Protocol

import pytest

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactScope,
)
from ai_code_change_agent.application.artifacts.ports import ArtifactStore

K = ArtifactKind
LIMIT = 1024
SCOPE = ArtifactScope(uuid.uuid4(), uuid.uuid4())
OTHER_ATTEMPT = ArtifactScope(SCOPE.request_id, uuid.uuid4())
OTHER_REQUEST = ArtifactScope(uuid.uuid4(), SCOPE.attempt_id)
MALFORMED_IDS = [
    "../x",
    "art_../../etc/passwd",
    "art_" + "g" * 32,
    "art_" + "0" * 31,
    "",
    "/etc/passwd",
    "ART_" + "0" * 32,
    "art_" + "0" * 32 + "/..",
]


class ArtifactBackdoor(Protocol):
    def replace_blob(self, meta: ArtifactMeta, data: bytes) -> None: ...

    def replace_meta(self, meta: ArtifactMeta, raw: bytes) -> None: ...

    def remove_meta(self, meta: ArtifactMeta) -> None: ...

    def copy_to_scope(self, meta: ArtifactMeta, other: ArtifactScope) -> None:
        """Copy the blob and metadata, unchanged, to the place `other` would keep them."""
        ...

    def add_unrelated(self, scope: ArtifactScope) -> None:
        """Store something that is not an artifact where the scope's artifacts are kept."""
        ...


class ArtifactStoreContract:
    async def test_store_and_retrieve_with_metadata(self, store: ArtifactStore) -> None:
        meta = await store.put(
            SCOPE, K.IMPLEMENTATION_PLAN, b'{"a":1}', redactions=2, redaction_version="9"
        )
        assert meta.id.startswith("art_") and len(meta.id) == 36 and meta.size == 7
        assert (meta.redactions, meta.redaction_version, meta.kind) == (
            2,
            "9",
            K.IMPLEMENTATION_PLAN,
        )
        assert await store.get(SCOPE, meta.id) == b'{"a":1}'
        assert await store.meta(SCOPE, meta.id) == meta
        assert await store.exists(SCOPE, meta.id)
        assert (await store.verify(SCOPE, meta.id)) == meta
        assert await store.list(SCOPE) == [meta]

    async def test_every_put_gets_a_new_opaque_id_and_nothing_is_overwritten(
        self, store: ArtifactStore
    ) -> None:
        a = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"same")
        b = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"same")
        assert a.id != b.id and a.sha256 == b.sha256
        assert await store.get(SCOPE, a.id) == await store.get(SCOPE, b.id) == b"same"

    async def test_tampered_blob_is_detected_on_every_read(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.CHANGESET, b"original")
        backdoor.replace_blob(meta, b"tampered")
        for call in (store.get, store.verify):
            with pytest.raises(ArtifactIntegrityError):
                await call(SCOPE, meta.id)

    async def test_same_size_tamper_is_detected_by_the_hash(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.CHANGESET, b"original")
        backdoor.replace_blob(meta, b"0riginal")  # same length
        with pytest.raises(ArtifactIntegrityError):
            await store.get(SCOPE, meta.id)

    async def test_a_blob_grown_past_the_limit_is_refused_on_read(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.CHANGESET, b"original")
        backdoor.replace_blob(meta, b"x" * (LIMIT + 1))
        with pytest.raises(ArtifactTooLargeError):
            await store.get(SCOPE, meta.id)

    async def test_corrupt_metadata_fails_closed(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
        backdoor.replace_meta(meta, b"{not json")
        with pytest.raises(ArtifactIntegrityError, match="artifact_metadata_invalid"):
            await store.meta(SCOPE, meta.id)

    async def test_artifacts_are_invisible_outside_their_request_and_attempt(
        self, store: ArtifactStore
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
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
        backdoor.copy_to_scope(meta, OTHER_ATTEMPT)
        with pytest.raises(ArtifactNotFoundError):  # metadata names the original scope
            await store.get(OTHER_ATTEMPT, meta.id)

    @pytest.mark.parametrize("bad_id", MALFORMED_IDS)
    async def test_malformed_ids_cannot_name_an_artifact(
        self, store: ArtifactStore, bad_id: str
    ) -> None:
        await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
        for call in (store.get, store.meta, store.verify):
            with pytest.raises(ArtifactNotFoundError):
                await call(SCOPE, bad_id)
        assert not await store.exists(SCOPE, bad_id)

    async def test_size_limit_is_enforced_on_write(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactTooLargeError):
            await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x" * (LIMIT + 1))
        assert await store.list(SCOPE) == []

    async def test_list_is_ordered_and_ignores_unrelated_entries(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        a = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"a")
        b = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"b")
        backdoor.add_unrelated(SCOPE)
        assert [m.id for m in await store.list(SCOPE)] == [a.id, b.id]

    async def test_concurrent_puts_get_distinct_ids(self, store: ArtifactStore) -> None:
        metas = await asyncio.gather(
            *(store.put(SCOPE, K.VALIDATION_EVIDENCE, b"e") for _ in range(8))
        )
        assert len({m.id for m in metas}) == 8
        assert sorted(m.id for m in await store.list(SCOPE)) == sorted(m.id for m in metas)

    async def test_blob_without_metadata_is_an_unreferenced_orphan(
        self, store: ArtifactStore, backdoor: ArtifactBackdoor
    ) -> None:
        meta = await store.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x")
        backdoor.remove_meta(meta)
        with pytest.raises(ArtifactNotFoundError):
            await store.get(SCOPE, meta.id)
        assert await store.list(SCOPE) == []
