"""The DynamoDB registry adapter against Floci (DYNAMODB-ACCESS-PATTERNS.md 5, R1-R20).

Scenarios ported from `tests/integration/test_registry_database.py` (which stays until PostgreSQL is
removed). Each test runs on its own `s2116-<uuid>` table through `RepositoryRegistryService` and
`RepositoryResolver` wired to `DynamoRegistryUnitOfWork`."""

import asyncio
import uuid
from collections.abc import Callable

import pytest

from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.repositories.errors import (
    ConflictError,
    InvalidStateError,
    RegistryValidationError,
)
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    AuditEvent,
    RepositoryStatus,
)
from ai_code_change_agent.domain.repositories.resolution import ResolutionOutcome as O
from ai_code_change_agent.domain.repositories.resolution import ResolutionRequest
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.codec import encode_json
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from tests.support.registry_fixtures import django_service, new_repo

pytestmark = [pytest.mark.floci, pytest.mark.integration]
ACTOR = "cli:itest"


def service(table: Table) -> RepositoryRegistryService:
    return RepositoryRegistryService(lambda: DynamoRegistryUnitOfWork(table))


def resolver(table: Table) -> RepositoryResolver:
    return RepositoryResolver(
        lambda: DynamoRegistryUnitOfWork(table), parsers=(GitHubReferenceParser(),)
    )


def uow(table: Table) -> DynamoRegistryUnitOfWork:
    return DynamoRegistryUnitOfWork(table)


async def onboard(table: Table, key: str = "django-service", project: str = "SHOP") -> None:
    svc = service(table)
    await svc.register_repository(new_repo(key), actor=ACTOR)
    await svc.create_version(key, {**django_service(), "repository_key": key}, actor=ACTOR)
    await svc.publish_version(key, 1, actor=ACTOR)
    await svc.map_project(project, key, actor=ACTOR)
    await svc.enable(key, actor=ACTOR)


async def all_events(table: Table) -> list[AuditEvent]:
    async with uow(table) as u:
        return await u.audit.list_events(limit=500)


def calls(table: Table, operation: str) -> list[str]:
    seen: list[str] = []
    table._client.meta.events.register(
        f"before-call.dynamodb.{operation}", lambda **_: seen.append(operation)
    )
    return seen


async def raises_code(code: str, action: Callable[[], object]) -> None:
    with pytest.raises(ConflictError) as raised:
        await action()  # type: ignore[misc]
    assert str(raised.value) == code
    assert not isinstance(raised.value, ConcurrentModificationError)


async def test_register_publish_map_resolve_disable_flow(app_table: Table) -> None:
    deletes = calls(app_table, "DeleteItem")
    await onboard(app_table)
    res = await resolver(app_table).resolve(
        ResolutionRequest("SHOP-1", "SHOP", ("django-service",))
    )
    assert res.outcome is O.RESOLVED and res.profile_version == 1
    for ref in ("acme/django-service", "https://github.com/ACME/django-service.git"):
        res = await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP", (ref,)))
        assert res.outcome is O.RESOLVED and res.repository_key == "django-service", ref
    res = await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP", ("acme/unknown",)))
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.detail == "reference_not_registered"
    assert (await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP"))).resolved

    await service(app_table).disable("django-service", actor=ACTOR)
    res = await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP"))
    assert res.outcome is O.DISABLED_REPOSITORY

    events = await all_events(app_table)
    assert {e.action for e in events} >= {
        AuditAction.REPOSITORY_REGISTERED,
        AuditAction.PROFILE_VERSION_CREATED,
        AuditAction.PROFILE_VERSION_PUBLISHED,
        AuditAction.PROFILE_VERSION_ACTIVATED,
        AuditAction.PROJECT_MAPPED,
        AuditAction.REPOSITORY_ENABLED,
        AuditAction.REPOSITORY_DISABLED,
    }
    assert all(e.actor == ACTOR for e in events)
    assert "app-installation" not in " ".join(str(e.metadata) for e in events)
    assert deletes == []  # R20: nothing in the registry is ever deleted


async def test_stored_items_carry_no_credential_and_the_hash(app_table: Table) -> None:
    await onboard(app_table)
    async with uow(app_table) as u:
        repo = await u.store.get_repository("django-service")
        assert repo is not None
        (version,) = await u.store.list_profile_versions(repo.id)
    assert repo.access_reference == "app-installation:12345"
    assert repo.status is RepositoryStatus.ACTIVE and repo.active_profile_version_id == version.id
    assert version.published and len(version.content_sha256) == 64


async def test_unique_constraints_and_conflict_codes(app_table: Table) -> None:
    svc = service(app_table)
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    for dup in (
        new_repo("shop-api", pid="x", name="x"),  # same key
        new_repo("other", name="shop-api"),  # same locator
        new_repo("third", pid="id-shop-api"),  # same provider identity
    ):
        await raises_code(
            "repository_already_registered",
            lambda dup=dup: svc.register_repository(dup, actor=ACTOR),  # type: ignore[misc]
        )
    await svc.register_repository(new_repo("billing"), actor=ACTOR)
    await svc.add_alias("shop-api", "storefront", actor=ACTOR)
    for key, alias in (("billing", "storefront"), ("billing", "shop-api"), ("shop-api", "billing")):
        await raises_code(
            "alias_taken",
            lambda key=key, alias=alias: svc.add_alias(key, alias, actor=ACTOR),  # type: ignore[misc]
        )
    # a key may not reuse another repository's alias
    await raises_code(
        "repository_key_taken",
        lambda: svc.register_repository(new_repo("storefront"), actor=ACTOR),
    )
    await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    await raises_code(
        "mapping_already_active", lambda: svc.map_project("SHOP", "shop-api", actor=ACTOR)
    )
    assert [r.repository_key for r in await svc.list_repositories()] == ["billing", "shop-api"]


async def test_a_failed_mutation_leaves_no_audit_event_and_no_item(app_table: Table) -> None:
    svc = service(app_table)
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    before = await all_events(app_table)

    with pytest.raises(ConflictError):
        await svc.register_repository(new_repo("shop-api2", pid="id-shop-api"), actor=ACTOR)

    assert await all_events(app_table) == before
    assert await app_table.get(keys.alias("shop-api2")) is None
    assert await app_table.get(keys.locator_guard("github:acme/shop-api2")) is None
    await svc.create_version(
        "shop-api", {**django_service(), "repository_key": "shop-api"}, actor=ACTOR
    )
    with pytest.raises(InvalidStateError):
        await svc.enable("shop-api", actor=ACTOR)
    assert [r.status for r in await svc.list_repositories()] == [RepositoryStatus.DISABLED]


async def test_two_registrations_of_one_locator_create_exactly_one(app_table: Table) -> None:
    svc = service(app_table)
    results = await asyncio.gather(
        *[svc.register_repository(new_repo("shop-api"), actor=ACTOR) for _ in range(2)],
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)
    assert str(failures[0]) == "repository_already_registered"
    assert len(await svc.list_repositories()) == 1
    assert len(await all_events(app_table)) == 1


async def test_concurrent_version_creation_numbers_from_the_repo_counter(app_table: Table) -> None:
    svc = service(app_table)
    await svc.register_repository(new_repo("django-service"), actor=ACTOR)
    doc = django_service()
    created = await asyncio.gather(
        *[svc.create_version("django-service", doc, actor=ACTOR) for _ in range(2)]
    )
    assert sorted(v.version for v in created) == [1, 2]
    _, versions, _, _ = await svc.show_repository("django-service")
    assert [v.version for v in versions] == [1, 2]
    repo = versions[0].repository_id
    stored = await app_table.get(keys.repository(repo))
    assert stored is not None and stored["last_profile_version"] == 2


async def test_concurrent_alias_claims_have_one_winner(app_table: Table) -> None:
    svc = service(app_table)
    for k in ("repo-a", "repo-b", "repo-c"):
        await svc.register_repository(new_repo(k), actor=ACTOR)
    results = await asyncio.gather(
        *[svc.add_alias(k, "shared-name", actor=ACTOR) for k in ("repo-a", "repo-b", "repo-c")],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(str(r) == "alias_taken" for r in results if isinstance(r, BaseException))


async def test_activation_of_a_draft_or_a_foreign_version_is_refused(app_table: Table) -> None:
    svc = service(app_table)
    for key in ("repo-one", "repo-two"):
        await svc.register_repository(new_repo(key), actor=ACTOR)
        await svc.create_version(key, {**django_service(), "repository_key": key}, actor=ACTOR)
    await svc.publish_version("repo-two", 1, actor=ACTOR, activate=False)
    with pytest.raises(InvalidStateError, match="version_not_published"):
        await svc.activate_version("repo-one", 1, actor=ACTOR)

    async with uow(app_table) as u:
        one = await u.store.get_repository("repo-one")
        two = await u.store.get_repository("repo-two")
        assert one is not None and two is not None
        draft = await u.store.get_profile_version(one.id, 1)
        foreign = await u.store.get_profile_version(two.id, 1)
        assert draft is not None and foreign is not None and foreign.published
    for version in (draft, foreign):
        async with uow(app_table) as u:
            await u.store.lock_repository("repo-one")
            with pytest.raises(ConflictError, match=r"^repository_state_conflict$"):
                await u.store.set_active_version(one.id, version.id)
    async with uow(app_table) as u:
        with pytest.raises(ConflictError, match=r"^repository_state_conflict$"):
            await u.store.set_status(one.id, RepositoryStatus.ACTIVE)  # no active version
    after = await service(app_table).show_repository("repo-one")
    assert after[0].active_profile_version_id is None


async def test_published_versions_are_immutable(app_table: Table) -> None:
    svc = service(app_table)
    await svc.register_repository(new_repo("django-service"), actor=ACTOR)
    await svc.create_version("django-service", django_service(), actor=ACTOR)
    # a publisher that read the draft loses to one that published it first
    async with uow(app_table) as slow:
        repo = await slow.store.get_repository("django-service")
        assert repo is not None
        draft = await slow.store.get_profile_version(repo.id, 1)
        assert draft is not None and not draft.published
        await svc.publish_version("django-service", 1, actor=ACTOR)
        published = await svc.show_repository("django-service")
        await slow.store.mark_published(draft.id)
        with pytest.raises(ConcurrentModificationError):
            await slow.commit()
    with pytest.raises(InvalidStateError, match="already_published"):
        await svc.publish_version("django-service", 1, actor=ACTOR)
    assert (await svc.show_repository("django-service"))[1] == published[1]


async def test_profile_tampered_in_storage_fails_resolution_closed(app_table: Table) -> None:
    await onboard(app_table)
    async with uow(app_table) as u:
        repo = await u.store.get_repository("django-service")
        assert repo is not None
    key = keys.profile_version(repo.id, 1)
    item = await app_table.get(key)
    assert item is not None
    content = django_service()
    content["commands"][1]["argv"] = ["bash", "-c", "npm"]
    await app_table.put({**item, "content_json": encode_json(content)})  # out-of-band write
    res = await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP"))
    assert res.outcome is O.PROFILE_INVALID and res.detail.startswith("revalidation_failed")


async def test_invalid_profile_is_rejected_before_any_write(app_table: Table) -> None:
    svc = service(app_table)
    await svc.register_repository(new_repo("django-service"), actor=ACTOR)
    bad = django_service()
    bad["commands"][1]["argv"] = ["curl", "http://evil.invalid"]
    with pytest.raises(RegistryValidationError):
        await svc.create_version("django-service", bad, actor=ACTOR)
    assert (await svc.show_repository("django-service"))[1] == []


async def test_read_paths_and_lifecycle_operations(app_table: Table) -> None:
    await onboard(app_table)
    svc = service(app_table)
    await svc.add_alias("django-service", "api", actor=ACTOR)
    await svc.create_version("django-service", {**django_service()}, actor=ACTOR)
    await svc.publish_version("django-service", 2, actor=ACTOR, activate=False)
    repo, versions, aliases, mappings = await svc.show_repository("django-service")
    assert [v.version for v in versions] == [1, 2] and all(v.published for v in versions)
    assert aliases == ["api", "django-service"]
    assert [m.jira_project_key for m in mappings] == ["SHOP"]
    assert repo.active_profile_version_id == versions[0].id and repo.row_version >= 3
    await svc.activate_version("django-service", 2, actor=ACTOR)
    await svc.activate_version("django-service", 1, actor=ACTOR)
    assert [m.status.value for m in await svc.list_mappings(project_key="SHOP")] == ["ACTIVE"]
    assert len(await svc.list_mappings(key="django-service")) == 1
    assert len(await svc.list_mappings()) == 1
    await svc.unmap_project("SHOP", "django-service", actor=ACTOR)
    await svc.map_project("SHOP", "django-service", actor=ACTOR)
    events = await svc.audit_history("django-service", limit=500)
    assert len(events) == len(await all_events(app_table))
    assert events[0].occurred_at is not None
    assert events[0].action is AuditAction.PROJECT_MAPPED  # newest first
    assert len(await svc.audit_history(None, limit=2)) == 2
    res = await resolver(app_table).resolve(ResolutionRequest("SHOP-1", "SHOP", ("api",)))
    assert res.profile_version == 1


async def test_list_events_with_both_filters_and_with_none(app_table: Table) -> None:
    svc = service(app_table)
    one = await svc.register_repository(new_repo("repo-one"), actor=ACTOR)
    two = await svc.register_repository(new_repo("repo-two"), actor=ACTOR)
    await svc.add_alias("repo-one", "first", actor=ACTOR)
    async with uow(app_table) as u:
        await u.audit.record(
            AuditEvent(AuditAction.REPOSITORY_REGISTERED, "system", None, ACTOR, {"k": 1})
        )
        await u.commit()

    async with uow(app_table) as u:
        mine = await u.audit.list_events(entity_type="repository", entity_id=one.id)
        theirs = await u.audit.list_events(entity_type="repository", entity_id=two.id)
        everything = await u.audit.list_events()
        newest = await u.audit.list_events(limit=1)
        by_type = await u.audit.list_events(entity_type="system")
        by_id = await u.audit.list_events(entity_id=two.id)
        unknown = await u.audit.list_events(entity_type="repository", entity_id=uuid.uuid4())
        none = await u.audit.list_events(limit=0)

    assert [e.action for e in mine] == [AuditAction.ALIAS_ADDED, AuditAction.REPOSITORY_REGISTERED]
    assert all(e.entity_id == one.id for e in mine)
    assert [e.entity_id for e in theirs] == [two.id]
    assert len(everything) == 4 and everything[0].entity_type == "system"
    stamps = [str(e.occurred_at) for e in everything]
    assert stamps == sorted(stamps, reverse=True)
    assert newest == everything[:1]
    assert [(e.entity_id, e.metadata) for e in by_type] == [(None, {"k": 1})]
    assert [e.entity_id for e in by_id] == [two.id]
    assert unknown == [] and none == []
