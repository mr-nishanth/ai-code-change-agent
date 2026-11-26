import json
from typing import Any

import pytest

from ai_code_change_agent.application.git_providers.ports import (
    GitProviderRegistry,
    ProviderRepositoryInfo,
)
from ai_code_change_agent.domain.repositories.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ProviderMismatchError,
    RegistryValidationError,
)
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    MappingStatus,
    RepositoryStatus,
)
from tests.support.registry_fixtures import nestjs_backend, new_repo, profile
from tests.support.registry_memory import InMemoryRegistry

ACTOR = "cli:tester"


class FakeProvider:
    def __init__(self, info: ProviderRepositoryInfo) -> None:
        self.info = info
        self.calls = 0

    async def describe_repository(
        self, *, owner: str, name: str, access_reference: str
    ) -> ProviderRepositoryInfo:
        self.calls += 1
        return self.info


async def ready(
    reg: InMemoryRegistry, key: str = "nest-backend", doc: dict[str, Any] | None = None
) -> None:
    svc = reg.service()
    await svc.register_repository(new_repo(key), actor=ACTOR)
    await svc.create_version(key, doc or {**nestjs_backend(), "repository_key": key}, actor=ACTOR)
    await svc.publish_version(key, 1, actor=ACTOR)


async def test_register_creates_disabled_repository_and_audit() -> None:
    reg = InMemoryRegistry()
    repo = await reg.service().register_repository(
        new_repo("shop-api"), actor=ACTOR, correlation_id="req-1"
    )
    assert repo.status is RepositoryStatus.DISABLED and repo.active_profile_version_id is None
    (event,) = reg.state.audit
    assert event.action is AuditAction.REPOSITORY_REGISTERED and event.actor == ACTOR
    assert event.correlation_id == "req-1" and event.metadata["provider_verified"] is False
    assert "app-installation" not in json.dumps(dict(event.metadata))


async def test_register_validates_input_and_actor() -> None:
    svc = InMemoryRegistry().service()
    with pytest.raises(RegistryValidationError):
        await svc.register_repository(
            new_repo("shop-api", ref="https://github.com/a/b.git"), actor=ACTOR
        )
    with pytest.raises(RegistryValidationError):
        await svc.register_repository(new_repo("shop-api"), actor="not an actor")


async def test_duplicates_are_conflicts_and_leave_no_partial_state() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    for dup in (
        new_repo("shop-api", pid="other", name="other"),
        new_repo("other-key", name="shop-api"),
        new_repo("third", pid="id-shop-api"),
    ):
        with pytest.raises(ConflictError):
            await svc.register_repository(dup, actor=ACTOR)
    assert len(reg.state.repos) == 1 and len(reg.state.audit) == 1


async def test_provider_verification_when_adapter_registered() -> None:
    reg = InMemoryRegistry()
    ok = FakeProvider(ProviderRepositoryInfo("id-shop-api", "Acme", "Shop-API", "main"))
    svc = reg.service(providers=GitProviderRegistry({"github": ok}))
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    assert ok.calls == 1 and reg.state.audit[0].metadata["provider_verified"] is True
    bad = FakeProvider(ProviderRepositoryInfo("someone-elses-id", "acme", "shop-api", "main"))
    with pytest.raises(ProviderMismatchError):
        await (
            InMemoryRegistry()
            .service(providers=GitProviderRegistry({"github": bad}))
            .register_repository(new_repo("shop-api"), actor=ACTOR)
        )


async def test_provider_is_separate_from_repository_identity() -> None:
    svc = InMemoryRegistry().service()
    a = await svc.register_repository(
        new_repo("shop-gh", provider="github", pid="1", name="shop"), actor=ACTOR
    )
    b = await svc.register_repository(
        new_repo("shop-gl", provider="gitlab", pid="1", name="shop"), actor=ACTOR
    )
    assert a.canonical_locator != b.canonical_locator and a.git_provider != b.git_provider


async def test_profile_version_lifecycle() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    v1 = await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    v2 = await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    assert (v1.version, v2.version) == (1, 2) and not v1.published
    pub = await svc.publish_version("nest-backend", 1, actor=ACTOR)
    assert pub.published
    repo, versions, _, _ = await svc.show_repository("nest-backend")
    assert repo.active_profile_version_id == v1.id and [v.published for v in versions] == [
        True,
        False,
    ]
    with pytest.raises(InvalidStateError):
        await svc.publish_version("nest-backend", 1, actor=ACTOR)
    with pytest.raises(InvalidStateError):
        await svc.activate_version("nest-backend", 2, actor=ACTOR)
    await svc.publish_version("nest-backend", 2, actor=ACTOR, activate=False)
    assert (await svc.show_repository("nest-backend"))[0].active_profile_version_id == v1.id
    await svc.activate_version("nest-backend", 2, actor=ACTOR)
    assert (await svc.show_repository("nest-backend"))[0].active_profile_version_id == v2.id
    await svc.activate_version(
        "nest-backend", 1, actor=ACTOR
    )  # rollback to an older published version
    assert (await svc.show_repository("nest-backend"))[0].active_profile_version_id == v1.id
    with pytest.raises(NotFoundError):
        await svc.publish_version("nest-backend", 9, actor=ACTOR)


async def test_create_version_rejects_wrong_key_and_invalid_profiles() -> None:
    svc = InMemoryRegistry().service()
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    with pytest.raises(RegistryValidationError) as exc:
        await svc.create_version("shop-api", nestjs_backend(), actor=ACTOR)
    assert exc.value.findings[0].code == "key_mismatch"
    with pytest.raises(RegistryValidationError):
        await svc.create_version(
            "shop-api", {**profile("shop-api"), "command_policy": "x"}, actor=ACTOR
        )
    with pytest.raises(NotFoundError):
        await svc.create_version("missing", profile("missing"), actor=ACTOR)


async def test_publish_revalidates_against_current_policy_and_rolls_back() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    v = await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    reg.state.versions[v.id].content["command_policy"] = "tampered"
    before = len(reg.state.audit)
    with pytest.raises(RegistryValidationError):
        await svc.publish_version("nest-backend", 1, actor=ACTOR)
    assert not reg.state.versions[v.id].published and len(reg.state.audit) == before


async def test_enable_disable_rules() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    with pytest.raises(InvalidStateError):
        await svc.enable("nest-backend", actor=ACTOR)  # no active profile
    with pytest.raises(InvalidStateError):
        await svc.disable("nest-backend", actor=ACTOR)
    await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    await svc.publish_version("nest-backend", 1, actor=ACTOR)
    assert (await svc.enable("nest-backend", actor=ACTOR)).status is RepositoryStatus.ACTIVE
    with pytest.raises(InvalidStateError):
        await svc.enable("nest-backend", actor=ACTOR)
    assert (await svc.disable("nest-backend", actor=ACTOR)).status is RepositoryStatus.DISABLED
    assert (
        await svc.enable("nest-backend", actor=ACTOR)
    ).status is RepositoryStatus.ACTIVE  # re-enable
    with pytest.raises(NotFoundError):
        await svc.enable("nope", actor=ACTOR)


async def test_aliases_share_one_namespace_with_keys() -> None:
    svc = InMemoryRegistry().service()
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    await svc.register_repository(new_repo("billing"), actor=ACTOR)
    assert await svc.add_alias("shop-api", "Storefront", actor=ACTOR) == "storefront"
    for key, alias in (
        ("billing", "storefront"),
        ("billing", "shop-api"),
        ("shop-api", "billing"),
        ("shop-api", "storefront"),
    ):
        with pytest.raises(ConflictError):
            await svc.add_alias(key, alias, actor=ACTOR)
    with pytest.raises(RegistryValidationError):
        await svc.add_alias("shop-api", "http://evil", actor=ACTOR)


async def test_project_mappings() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    m = await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    assert m.status is MappingStatus.ACTIVE
    with pytest.raises(ConflictError):
        await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    with pytest.raises(RegistryValidationError):
        await svc.map_project("shop", "shop-api", actor=ACTOR)
    assert (
        await svc.unmap_project("SHOP", "shop-api", actor=ACTOR)
    ).status is MappingStatus.DISABLED
    with pytest.raises(InvalidStateError):
        await svc.unmap_project("SHOP", "shop-api", actor=ACTOR)
    assert (
        await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    ).status is MappingStatus.ACTIVE  # reactivation
    assert len(await svc.list_mappings(project_key="SHOP")) == 1
    assert await svc.list_mappings(key="shop-api")
    with pytest.raises(NotFoundError):
        await svc.map_project("SHOP", "missing", actor=ACTOR)


async def test_audit_trail_covers_every_mutation_and_holds_no_secrets() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await ready(reg)
    await svc.add_alias("nest-backend", "nest", actor=ACTOR)
    await svc.map_project("SHOP", "nest-backend", actor=ACTOR)
    await svc.enable("nest-backend", actor=ACTOR)
    await svc.disable("nest-backend", actor=ACTOR)
    await svc.unmap_project("SHOP", "nest-backend", actor=ACTOR)
    actions = {e.action for e in reg.state.audit}
    assert actions == set(AuditAction)
    blob = json.dumps([dict(e.metadata) for e in reg.state.audit])
    assert "app-installation" not in blob and "12345" not in blob
    assert len(await svc.audit_history("nest-backend")) == len(reg.state.audit)
    assert len(await svc.audit_history(None, limit=2)) == 2
    assert [r.repository_key for r in await svc.list_repositories()] == ["nest-backend"]
    with pytest.raises(NotFoundError):
        await svc.audit_history("missing")
