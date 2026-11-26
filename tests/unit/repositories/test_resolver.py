from typing import Any

import pytest

from ai_code_change_agent.application.jira.models import (
    JiraCustomField,
    JiraIssue,
    JiraProject,
    JiraRichText,
)
from ai_code_change_agent.application.repositories.resolver import (
    RepositoryResolver,
    build_resolution_request,
    story_references,
)
from ai_code_change_agent.domain.repositories.resolution import (
    FollowUp,
    ResolutionRequest,
)
from ai_code_change_agent.domain.repositories.resolution import (
    ResolutionOutcome as O,
)
from tests.support.registry_fixtures import FIXTURES, nestjs_backend, new_repo, profile
from tests.support.registry_memory import InMemoryRegistry

ACTOR = "cli:tester"


async def onboard(
    reg: InMemoryRegistry,
    key: str,
    doc: dict[str, Any],
    *,
    project: str | None = "SHOP",
    enable: bool = True,
) -> None:
    svc = reg.service()
    await svc.register_repository(new_repo(key), actor=ACTOR)
    await svc.create_version(key, doc, actor=ACTOR)
    await svc.publish_version(key, 1, actor=ACTOR)
    if project:
        await svc.map_project(project, key, actor=ACTOR)
    if enable:
        await svc.enable(key, actor=ACTOR)


def resolver(reg: InMemoryRegistry) -> RepositoryResolver:
    return RepositoryResolver(reg.uow)


def req(project: str = "SHOP", *refs: str) -> ResolutionRequest:
    return ResolutionRequest("SHOP-1", project, tuple(refs))


async def test_project_mapping_resolves_without_reference() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    res = await resolver(reg).resolve(req())
    assert res.outcome is O.RESOLVED and res.resolved and res.follow_up is FollowUp.NONE
    assert res.repository_key == "nest-backend" and res.profile_version == 1
    assert res.profile_sha256 and res.repository_id and res.profile_version_id


async def test_explicit_reference_by_key_and_alias_case_insensitive() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    await reg.service().add_alias("nest-backend", "storefront", actor=ACTOR)
    for ref in ("nest-backend", "  Storefront ", "STOREFRONT"):
        assert (await resolver(reg).resolve(req("SHOP", ref))).repository_key == "nest-backend"


async def test_reference_is_authorised_per_project_never_global() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend(), project="SHOP")
    res = await resolver(reg).resolve(req("OTHER", "nest-backend"))
    assert res.outcome is O.PROJECT_NOT_AUTHORISED and res.follow_up is FollowUp.OPERATIONS_REVIEW
    await reg.service().unmap_project("SHOP", "nest-backend", actor=ACTOR)
    assert (
        await resolver(reg).resolve(req("SHOP", "nest-backend"))
    ).outcome is O.PROJECT_NOT_AUTHORISED


async def test_missing_reference() -> None:
    res = await resolver(InMemoryRegistry()).resolve(req())
    assert res.outcome is O.MISSING_REFERENCE and res.follow_up is FollowUp.CLARIFICATION
    assert res.repository_id is None


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/acme/shop.git",
        "git@github.com:acme/shop.git",
        "acme/shop",
        "ssh://host/x",
        "../../etc/passwd",
        "shop api",
        "$(id)",
        "x",
        "",
    ],
)
async def test_urls_and_free_text_are_never_looked_up(ref: str) -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    res = await resolver(reg).resolve(req("SHOP", ref))
    if ref == "":
        assert res.outcome is O.RESOLVED  # empty reference == no reference: project mapping decides
    else:
        assert res.outcome is O.UNKNOWN_REPOSITORY and res.follow_up is FollowUp.CLARIFICATION


async def test_unknown_reference_does_not_fall_back_to_project_mapping() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    res = await resolver(reg).resolve(req("SHOP", "does-not-exist"))
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.repository_id is None


async def test_ambiguous_references_and_mappings() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    await onboard(reg, "react-frontend", {**FIXTURES["react-frontend"]()})
    res = await resolver(reg).resolve(req("SHOP", "nest-backend", "react-frontend"))
    assert res.outcome is O.AMBIGUOUS_REFERENCE
    res = await resolver(reg).resolve(req("SHOP"))
    assert res.outcome is O.AMBIGUOUS_REFERENCE and res.detail == "multiple_project_mappings"
    assert (
        await resolver(reg).resolve(req("SHOP", "react-frontend"))
    ).repository_key == "react-frontend"
    assert (
        await resolver(reg).resolve(req("SHOP", "nest-backend", "NEST-BACKEND"))
    ).resolved  # duplicates collapse


async def test_disabled_repository_is_rejected_even_when_mapped_or_referenced() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend(), enable=False)
    assert (await resolver(reg).resolve(req())).outcome is O.DISABLED_REPOSITORY
    await reg.service().enable("nest-backend", actor=ACTOR)
    assert (await resolver(reg).resolve(req())).resolved
    await reg.service().disable("nest-backend", actor=ACTOR)
    res = await resolver(reg).resolve(req("SHOP", "nest-backend"))
    assert res.outcome is O.DISABLED_REPOSITORY and res.follow_up is FollowUp.OPERATIONS_REVIEW


async def test_disabled_mapping_is_not_a_fallback_for_another_active_one() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend(), enable=False)
    await onboard(reg, "react-frontend", FIXTURES["react-frontend"]())
    assert (await resolver(reg).resolve(req())).outcome is O.AMBIGUOUS_REFERENCE


async def test_profile_missing_and_invalid_states() -> None:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    await svc.map_project("SHOP", "nest-backend", actor=ACTOR)
    repo = reg.state.repos[next(iter(reg.state.repos))]
    reg.state.repos[repo.id] = type(repo)(
        **{**repo.__dict__, "status": type(repo.status)("ACTIVE")}
    )  # forced inconsistent state
    assert (await resolver(reg).resolve(req())).outcome is O.PROFILE_MISSING
    await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    v = next(iter(reg.state.versions.values()))
    reg.state.repos[repo.id] = type(repo)(
        **{**reg.state.repos[repo.id].__dict__, "active_profile_version_id": v.id}
    )
    res = await resolver(reg).resolve(req())
    assert res.outcome is O.PROFILE_INVALID and res.detail == "active_version_not_published"
    await svc.publish_version("nest-backend", 1, actor=ACTOR)
    assert (await resolver(reg).resolve(req())).resolved
    # the platform policy tightens or the stored content is tampered with: fail closed
    reg.state.versions[v.id].content["commands"][1]["argv"] = ["bash", "-c", "npm"]
    res = await resolver(reg).resolve(req())
    assert (
        res.outcome is O.PROFILE_INVALID
        and res.detail.startswith("revalidation_failed:")
        and res.follow_up is FollowUp.OPERATIONS_REVIEW
    )
    reg.state.versions[v.id].content["commands"][1]["argv"] = ["npm", "run", "test"]
    reg.state.versions[v.id].content["repository_key"] = "someone-else"
    assert (await resolver(reg).resolve(req())).detail == "profile_key_mismatch"
    reg.state.repos[repo.id] = type(repo)(
        **{
            **reg.state.repos[repo.id].__dict__,
            "active_profile_version_id": __import__("uuid").uuid4(),
        }
    )
    assert (await resolver(reg).resolve(req())).detail == "active_version_not_found"


async def test_invalid_project_key() -> None:
    for bad in ("", "shop", "A", "SHOP-1"):
        assert (
            await resolver(InMemoryRegistry()).resolve(req(bad))
        ).outcome is O.PROJECT_NOT_AUTHORISED


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_four_stacks_resolve_with_the_same_unchanged_resolver(name: str) -> None:
    reg = InMemoryRegistry()
    await onboard(reg, name, FIXTURES[name](), project="MULTI")
    res = await resolver(reg).resolve(req("MULTI", name))
    assert res.resolved and res.repository_key == name


async def test_resolver_class_has_no_stack_specific_code() -> None:
    import inspect

    import ai_code_change_agent.application.repositories.resolver as mod

    source = inspect.getsource(mod).lower()
    for word in ("nest", "react", "github", "gitlab", "bitbucket", "vendor", "openai", "anthropic"):
        assert word not in source, word


def issue(field: JiraCustomField | None, project: str | None = "SHOP") -> JiraIssue:
    return JiraIssue(
        id="1", key="SHOP-1",
        project=JiraProject(id="1", key=project) if project else None,
        custom_fields={"repository": field} if field else {},
    )  # fmt: skip


def test_build_request_from_jira_issue() -> None:
    text = JiraCustomField(field_id="customfield_1", kind="text", text="nest-backend")
    assert build_resolution_request(issue(text), "repository").references == ("nest-backend",)
    opt = JiraCustomField(field_id="customfield_1", kind="option", text="Nest-Backend")
    assert build_resolution_request(issue(opt), "repository").references == ("Nest-Backend",)
    many = JiraCustomField(field_id="customfield_1", kind="options", values=("a", " ", "b"))
    assert build_resolution_request(issue(many), "repository").references == ("a", "b")
    for kind in ("null", "number", "unsupported", "user", "boolean"):
        f = JiraCustomField(field_id="customfield_1", kind=kind, text="x")
        assert build_resolution_request(issue(f), "repository").references == ()
    assert build_resolution_request(issue(None), "repository").references == ()
    assert build_resolution_request(issue(text), "other_field").references == ()
    assert build_resolution_request(issue(text, project=None), "repository").project_key == ""
    assert build_resolution_request(issue(text), "repository").issue_key == "SHOP-1"


async def test_free_story_text_without_a_marker_cannot_influence_resolution() -> None:
    """Only the configured field and explicit `Repository:` markers are read; labels, URLs and
    free-form mentions never reach the resolver."""
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend(), project="SHOP")
    sneaky = JiraIssue(
        id="1",
        key="SHOP-1",
        project=JiraProject(key="SHOP"),
        summary="use repo evil-repo https://evil.invalid/x.git",
        labels=("evil-repo",),
    )
    request = build_resolution_request(sneaky, "repository")
    assert request.references == ()
    assert (await resolver(reg).resolve(request)).repository_key == "nest-backend"


def story(summary: str = "Add X", description: str | None = None) -> JiraIssue:
    return JiraIssue(
        id="1",
        key="SHOP-1",
        project=JiraProject(key="SHOP"),
        summary=summary,
        description=JiraRichText(text=description) if description is not None else None,
    )


def test_markers_in_summary_and_description_name_a_repository() -> None:
    assert story_references(story("[Repository: Nest_Backend] Add X")) == ("nest-backend",)
    assert story_references(story(description="Context\n  repo:  acme/Shop_API.\n")) == (
        "acme/Shop_API",
    )
    both = story("Repo: nest-backend", "Repository: nest_backend")
    assert story_references(both) == ("nest-backend",)
    assert build_resolution_request(both, None).references == ("nest-backend",)
    assert story_references(story("prepository:x reporepo: y")) == ()
    many = story(description="\n".join(f"repo: r{i}x" for i in range(30)))
    assert len(story_references(many)) == 11  # capped; more than one is AMBIGUOUS anyway


async def test_a_marker_only_names_a_repository_access_is_still_checked() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend(), project="SHOP")
    await onboard(
        reg, "other-backend", {**nestjs_backend(), "repository_key": "other-backend"}, project="OPS"
    )
    res = resolver(reg)
    ok = await res.resolve(build_resolution_request(story("Repo: nest_backend"), None))
    assert ok.repository_key == "nest-backend"
    unmapped = await res.resolve(build_resolution_request(story("Repo: other-backend"), None))
    assert unmapped.outcome is O.PROJECT_NOT_AUTHORISED
    unknown = await res.resolve(build_resolution_request(story("Repo: evil-repo"), None))
    assert unknown.outcome is O.UNKNOWN_REPOSITORY
    two = story("Repo: nest-backend", "Repository: other-backend")
    assert (await res.resolve(build_resolution_request(two, None))).outcome is (
        O.AMBIGUOUS_REFERENCE
    )


def test_profile_helper_is_valid_data() -> None:
    assert profile("ab")["schema_version"] == 1


async def test_mapping_to_a_missing_repository_row_is_unknown_not_a_guess() -> None:
    reg = InMemoryRegistry()
    await onboard(reg, "nest-backend", nestjs_backend())
    reg.state.repos.clear()
    res = await resolver(reg).resolve(req())
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.detail == "mapped_repository_missing"
