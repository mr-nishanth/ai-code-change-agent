"""Gate D.2: Story references by alias, owner/name or GitHub URL; only registered repositories."""

import pytest

from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.domain.repositories.resolution import ResolutionOutcome as O
from ai_code_change_agent.domain.repositories.resolution import ResolutionRequest
from ai_code_change_agent.integrations.github.references import (
    GitHubReferenceParser,
)
from tests.support.registry_fixtures import django_service, new_repo
from tests.support.registry_memory import InMemoryRegistry

ACTOR = "cli:tester"
P = GitHubReferenceParser()


@pytest.mark.parametrize(
    "value",
    [
        "acme/shop_service",
        "https://github.com/acme/shop_service",
        "https://github.com/acme/shop_service/",
        "https://github.com/acme/shop_service.git",
        "acme/shop_service.git",
        "  HTTPS://GitHub.com/Acme/Shop_Service  ",
    ],
)
def test_accepted_forms_normalise_to_one_locator(value: str) -> None:
    assert P.locator(value) == "github:acme/shop_service"


@pytest.mark.parametrize(
    "value",
    [
        "git@github.com:acme/shop.git", "ssh://git@github.com/acme/shop", "git://github.com/a/b",
        "http://github.com/acme/shop", "https://gitlab.com/acme/shop",
        "https://www.github.com/acme/shop", "https://api.github.com/repos/acme/shop",
        "https://github.com.evil.example/acme/shop", "https://evil.example/github.com/acme/shop",
        "https://user:token@github.com/acme/shop", "https://github.com:443/acme/shop",
        "https://github.com/acme/shop?ref=main", "https://github.com/acme/shop#readme",
        "https://github.com/acme/shop/tree/main", "https://github.com/acme/shop/blob/main/x.py",
        "https://github.com/acme/shop/pull/1", "https://github.com/acme", "https://github.com/",
        "acme/shop/extra", "acme/..", "acme/.", "../shop", "acme/sh op", "acme/shop%2F..",
        "acme\\shop", "-acme/shop", "acme/", "/shop", "file:///etc/passwd", "acme/.git",
        "a" * 40 + "/shop", "acme/" + "s" * 101, "acme/shop\nother/repo", "",
    ],
)  # fmt: skip
def test_unsupported_or_unsafe_references_are_refused(value: str) -> None:
    assert P.locator(value) is None


async def onboard(reg: InMemoryRegistry, *, enable: bool = True, project: str = "SHOP") -> None:
    svc = reg.service()
    repo = new_repo("shop-service", owner="acme", name="shop_service",
                    pid="1468634307")  # fmt: skip
    await svc.register_repository(repo, actor=ACTOR)
    await svc.create_version("shop-service", django_service() | {
        "repository_key": "shop-service"}, actor=ACTOR)  # fmt: skip
    await svc.publish_version("shop-service", 1, actor=ACTOR)
    await svc.map_project(project, "shop-service", actor=ACTOR)
    if enable:
        await svc.enable("shop-service", actor=ACTOR)


def resolver(reg: InMemoryRegistry) -> RepositoryResolver:
    return RepositoryResolver(reg.uow, parsers=(P,))


def req(ref: str, project: str = "SHOP") -> ResolutionRequest:
    return ResolutionRequest("SHOP-1", project, (ref,))


@pytest.mark.parametrize(
    "ref",
    ["shop-service", "acme/shop_service",
     "https://github.com/acme/shop_service",
     "https://github.com/acme/shop_service.git"],
)  # fmt: skip
async def test_every_accepted_form_resolves_the_registered_repository(ref: str) -> None:
    reg = InMemoryRegistry()
    await onboard(reg)
    res = await resolver(reg).resolve(req(ref))
    assert res.outcome is O.RESOLVED and res.repository_key == "shop-service"


async def test_a_repository_that_is_not_registered_is_never_resolved() -> None:
    reg = InMemoryRegistry()
    await onboard(reg)
    res = await resolver(reg).resolve(req("https://github.com/acme/billing_service"))
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.detail == "reference_not_registered"
    async with reg.uow() as uow:
        assert len(await uow.store.list_repositories()) == 1  # no automatic registration


async def test_an_unsupported_reference_never_reaches_a_lookup() -> None:
    reg = InMemoryRegistry()
    await onboard(reg)
    res = await resolver(reg).resolve(req("git@github.com:acme/shop_service.git"))
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.detail == "reference_unsupported"
    plain = await RepositoryResolver(reg.uow).resolve(req("acme/shop_service"))
    assert plain.detail == "reference_unsupported"  # no parser registered: nothing accepted


async def test_a_url_grants_nothing_without_registry_authorisation() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, enable=False)
    url = "https://github.com/acme/shop_service"
    assert (await resolver(reg).resolve(req(url))).outcome is O.DISABLED_REPOSITORY
    reg2 = InMemoryRegistry()
    await onboard(reg2, project="OTHER")
    assert (await resolver(reg2).resolve(req(url))).outcome is O.PROJECT_NOT_AUTHORISED
