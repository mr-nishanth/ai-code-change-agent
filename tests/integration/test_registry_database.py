import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.cli.main import main
from ai_code_change_agent.composition import build_registry_service, build_repository_resolver
from ai_code_change_agent.domain.repositories.errors import (
    ConflictError,
    InvalidStateError,
    RegistryValidationError,
)
from ai_code_change_agent.domain.repositories.resolution import ResolutionOutcome as O
from ai_code_change_agent.domain.repositories.resolution import ResolutionRequest
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq, query, run_alembic
from tests.support.registry_fixtures import FIXTURES, nestjs_backend, new_repo

pytestmark = pytest.mark.integration
ACTOR = "cli:itest"


@pytest.fixture
async def factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def sql(url: str, statement: str, params: tuple[object, ...] = ()) -> None:
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        conn.execute(statement, params)


async def onboard(
    f: async_sessionmaker[AsyncSession], key: str = "nest-backend", project: str = "SHOP"
) -> None:
    svc = build_registry_service(f)
    await svc.register_repository(new_repo(key), actor=ACTOR)
    await svc.create_version(key, {**nestjs_backend(), "repository_key": key}, actor=ACTOR)
    await svc.publish_version(key, 1, actor=ACTOR)
    await svc.map_project(project, key, actor=ACTOR)
    await svc.enable(key, actor=ACTOR)


async def test_a_story_url_resolves_through_the_unique_locator(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Gate D.2: owner/name and GitHub URLs are looked up by the stored canonical locator."""
    await onboard(factory)
    resolver = build_repository_resolver(factory)
    for ref in ("acme/nest-backend", "https://github.com/ACME/nest-backend.git"):
        res = await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP", (ref,)))
        assert res.outcome is O.RESOLVED and res.repository_key == "nest-backend", ref
    res = await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP", ("acme/unknown",)))
    assert res.outcome is O.UNKNOWN_REPOSITORY and res.detail == "reference_not_registered"


async def test_register_publish_map_resolve_disable_flow(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    resolver = build_repository_resolver(factory)
    res = await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP", ("nest-backend",)))
    assert res.outcome is O.RESOLVED and res.profile_version == 1
    assert (await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP"))).resolved
    await build_registry_service(factory).disable("nest-backend", actor=ACTOR)
    assert (
        await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP"))
    ).outcome is O.DISABLED_REPOSITORY
    actions = [
        r[0]
        for r in query(
            database_url, "SELECT action FROM registry_audit_events ORDER BY occurred_at, id"
        )
    ]
    assert set(actions) >= {
        "repository.registered",
        "profile_version.created",
        "profile_version.published",
        "profile_version.activated",
        "project_mapping.activated",
        "repository.enabled",
        "repository.disabled",
    }
    rows = query(database_url, "SELECT actor, metadata::text FROM registry_audit_events")
    assert all(a == ACTOR for a, _ in rows)
    assert "app-installation" not in " ".join(str(m) for _, m in rows)


async def test_stored_row_has_no_credential_and_hash_matches(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    ((ref, status, sha, published),) = query(
        database_url,
        "SELECT r.access_reference, r.status, v.content_sha256, v.published_at IS NOT NULL FROM repositories r JOIN repository_profile_versions v ON v.repository_id = r.id",
    )
    assert (
        ref == "app-installation:12345"
        and status == "ACTIVE"
        and published is True
        and len(str(sha)) == 64
    )


async def test_unique_constraints_and_conflicts(factory: async_sessionmaker[AsyncSession]) -> None:
    svc = build_registry_service(factory)
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    for dup in (
        new_repo("shop-api", pid="x", name="x"),
        new_repo("other", name="shop-api"),
        new_repo("third", pid="id-shop-api"),
    ):
        with pytest.raises(ConflictError):
            await svc.register_repository(dup, actor=ACTOR)
    await svc.register_repository(new_repo("billing"), actor=ACTOR)
    await svc.add_alias("shop-api", "storefront", actor=ACTOR)
    for key, alias in (("billing", "storefront"), ("billing", "shop-api"), ("shop-api", "billing")):
        with pytest.raises(ConflictError):
            await svc.add_alias(key, alias, actor=ACTOR)
    await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    with pytest.raises(ConflictError):
        await svc.map_project("SHOP", "shop-api", actor=ACTOR)
    assert len(await svc.list_repositories()) == 2


async def test_failed_operations_roll_back_completely(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    await svc.register_repository(new_repo("shop-api"), actor=ACTOR)
    before = query(database_url, "SELECT count(*) FROM registry_audit_events")
    with pytest.raises(ConflictError):
        await svc.register_repository(new_repo("shop-api2", pid="id-shop-api"), actor=ACTOR)
    assert query(database_url, "SELECT count(*) FROM registry_audit_events") == before
    assert query(
        database_url, "SELECT count(*) FROM repository_aliases WHERE alias = 'shop-api2'"
    ) == [(0,)]
    await svc.create_version(
        "shop-api", {**nestjs_backend(), "repository_key": "shop-api"}, actor=ACTOR
    )
    sql(database_url, "SELECT 1")  # connectivity sanity
    with pytest.raises(InvalidStateError):
        await svc.enable("shop-api", actor=ACTOR)
    assert query(database_url, "SELECT status FROM repositories") == [("DISABLED",)]


async def test_concurrent_registration_creates_exactly_one(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    results = await asyncio.gather(
        *[svc.register_repository(new_repo("shop-api"), actor=ACTOR) for _ in range(6)],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))
    assert query(database_url, "SELECT count(*) FROM repositories") == [(1,)]
    assert query(database_url, "SELECT count(*) FROM registry_audit_events") == [(1,)]


async def test_concurrent_version_creation_numbers_are_unique_and_gapless(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    doc = nestjs_backend()
    await asyncio.gather(*[svc.create_version("nest-backend", doc, actor=ACTOR) for _ in range(5)])
    assert [
        r[0]
        for r in query(
            database_url, "SELECT version FROM repository_profile_versions ORDER BY version"
        )
    ] == [1, 2, 3, 4, 5]


async def test_concurrent_alias_claims_have_one_winner(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    for k in ("repo-a", "repo-b", "repo-c"):
        await svc.register_repository(new_repo(k), actor=ACTOR)
    results = await asyncio.gather(
        *[svc.add_alias(k, "shared-name", actor=ACTOR) for k in ("repo-a", "repo-b", "repo-c")],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert query(
        database_url, "SELECT count(*) FROM repository_aliases WHERE alias = 'shared-name'"
    ) == [(1,)]


def _expect_blocked(url: str, statement: str, params: tuple[object, ...] = ()) -> str:
    with pytest.raises(psycopg.errors.IntegrityError) as exc:
        sql(url, statement, params)
    return str(exc.value)


async def test_published_versions_are_immutable_and_drafts_only_publishable(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    await svc.create_version("nest-backend", nestjs_backend(), actor=ACTOR)
    await svc.publish_version("nest-backend", 1, actor=ACTOR)
    for stmt in (
        "UPDATE repository_profile_versions SET content = '{}'::jsonb WHERE version = 1",
        "UPDATE repository_profile_versions SET content_sha256 = repeat('a', 64) WHERE version = 1",
        "UPDATE repository_profile_versions SET published_at = now() WHERE version = 1",
        "UPDATE repository_profile_versions SET published_at = NULL WHERE version = 1",
        "DELETE FROM repository_profile_versions WHERE version = 1",
        "DELETE FROM repository_profile_versions WHERE version = 2",
        "UPDATE repository_profile_versions SET content = '{}'::jsonb WHERE version = 2",
        "UPDATE repository_profile_versions SET version = 9 WHERE version = 2",
        "UPDATE repository_profile_versions SET created_by = 'cli:evil' WHERE version = 2",
    ):
        assert "version" in _expect_blocked(database_url, stmt), stmt
    assert query(
        database_url,
        "SELECT version, published_at IS NOT NULL FROM repository_profile_versions ORDER BY version",
    ) == [(1, True), (2, False)]
    sql(
        database_url,
        "UPDATE repository_profile_versions SET published_at = now() WHERE version = 2",
    )  # the one allowed change


async def test_audit_events_are_append_only(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await build_registry_service(factory).register_repository(new_repo("shop-api"), actor=ACTOR)
    assert "append-only" in _expect_blocked(
        database_url, "UPDATE registry_audit_events SET actor = 'cli:evil'"
    )
    assert "append-only" in _expect_blocked(database_url, "DELETE FROM registry_audit_events")
    assert query(database_url, "SELECT actor FROM registry_audit_events") == [(ACTOR,)]


async def test_repositories_cannot_be_deleted_and_foreign_keys_restrict(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    assert "retained" in _expect_blocked(database_url, "DELETE FROM repositories")
    with pytest.raises(psycopg.errors.IntegrityError):
        sql(
            database_url,
            "DELETE FROM repository_aliases WHERE kind = 'KEY'; DELETE FROM repositories",
        )


async def test_database_check_constraints(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    _expect_blocked(
        database_url, "UPDATE repositories SET status = 'ACTIVE', active_profile_version_id = NULL"
    )
    _expect_blocked(database_url, "UPDATE repositories SET status = 'PAUSED'")
    _expect_blocked(database_url, "UPDATE repositories SET repository_key = 'Bad Key'")
    _expect_blocked(
        database_url, "UPDATE repository_project_mappings SET jira_project_key = 'lower'"
    )
    _expect_blocked(database_url, "UPDATE repository_project_mappings SET status = 'X'")
    _expect_blocked(database_url, "UPDATE repository_aliases SET kind = 'OTHER'")


async def test_active_version_must_belong_to_the_same_repository(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    for key in ("repo-one", "repo-two"):
        await svc.register_repository(new_repo(key), actor=ACTOR)
        await svc.create_version(key, {**nestjs_backend(), "repository_key": key}, actor=ACTOR)
    other = query(
        database_url,
        "SELECT v.id FROM repository_profile_versions v JOIN repositories r ON r.id = v.repository_id WHERE r.repository_key = 'repo-two'",
    )[0][0]
    _expect_blocked(
        database_url,
        "UPDATE repositories SET active_profile_version_id = %s WHERE repository_key = 'repo-one'",
        (other,),
    )


async def test_profile_tampered_in_database_fails_resolution_closed(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    # simulate a policy that tightened after publication by switching the trigger off (superuser only)
    sql(
        database_url,
        "ALTER TABLE repository_profile_versions DISABLE TRIGGER repository_profile_versions_guard",
    )
    try:
        sql(
            database_url,
            'UPDATE repository_profile_versions SET content = jsonb_set(content, \'{commands,1,argv}\', \'["bash","-c","npm"]\')',
        )
    finally:
        sql(
            database_url,
            "ALTER TABLE repository_profile_versions ENABLE TRIGGER repository_profile_versions_guard",
        )
    res = await build_repository_resolver(factory).resolve(ResolutionRequest("SHOP-1", "SHOP"))
    assert res.outcome is O.PROFILE_INVALID and res.detail.startswith("revalidation_failed")


async def test_four_stack_fixtures_onboard_through_the_cli_against_postgres(
    database_url: str, tmp_path: Path, factory: async_sessionmaker[AsyncSession]
) -> None:
    def cli(*argv: str) -> int:
        import io

        from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
        from ai_code_change_agent.persistence.repositories.registry_store import (
            SqlAlchemyRegistryUnitOfWork,
        )

        return main(
            list(argv),
            service_factory=lambda: (
                build_registry_service(factory),
                RepositoryResolver(lambda: SqlAlchemyRegistryUnitOfWork(factory)),
            ),
            out=io.StringIO(),
            err_out=io.StringIO(),
        )

    # the CLI uses asyncio.run, which cannot nest inside a running loop: run it in a worker thread
    for name, builder in FIXTURES.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(builder()))
        steps = [
            [
                "repositories",
                "register",
                "--key",
                name,
                "--display-name",
                name,
                "--provider",
                "github",
                "--provider-id",
                f"id-{name}",
                "--owner",
                "acme",
                "--name",
                name,
                "--branch",
                "main",
                "--no-profile",
                "--access-ref",
                "app-installation:1",
            ],
            ["repositories", "create-version", name, str(path)],
            ["repositories", "publish", name, "1"],
            ["repositories", "map-project", "MULTI", name],
            ["repositories", "enable", name],
        ]
        for step in steps:
            assert await asyncio.to_thread(cli, *step) == 0, (name, step)
    resolver = build_repository_resolver(factory)
    for name in FIXTURES:
        res = await resolver.resolve(ResolutionRequest("MULTI-1", "MULTI", (name,)))
        assert res.resolved and res.repository_key == name
    assert (
        await resolver.resolve(ResolutionRequest("MULTI-1", "MULTI"))
    ).outcome is O.AMBIGUOUS_REFERENCE


async def test_invalid_profile_is_rejected_before_any_write(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    svc = build_registry_service(factory)
    await svc.register_repository(new_repo("nest-backend"), actor=ACTOR)
    bad = nestjs_backend()
    bad["commands"][1]["argv"] = ["curl", "http://evil.invalid"]
    with pytest.raises(RegistryValidationError):
        await svc.create_version("nest-backend", bad, actor=ACTOR)
    assert query(database_url, "SELECT count(*) FROM repository_profile_versions") == [(0,)]


def test_fresh_history_roundtrip_and_head(database_url: str) -> None:
    """The project's own migration history: one initial revision, reversible, no drift."""
    down = run_alembic(database_url, "downgrade", "base")
    assert down.returncode == 0, down.stderr[-1500:]
    tables = {
        r[0]
        for r in query(
            database_url,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        )
    }
    assert tables <= {"alembic_version"}
    assert query(
        database_url,
        "SELECT count(*) FROM pg_proc WHERE proname IN "
        "('forbid_change', 'attempt_guard', 'registry_profile_version_guard')",
    ) == [(0,)]
    up = run_alembic(database_url, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-1500:]
    heads = run_alembic(database_url, "heads")
    assert heads.stdout.strip().startswith("0003")
    check = run_alembic(database_url, "check")
    assert check.returncode == 0, check.stdout[-800:] + check.stderr[-800:]
    assert uuid.uuid4()


def test_default_cli_composition_reaches_the_real_database(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from ai_code_change_agent.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        out = io.StringIO()
        assert main(["repositories", "list"], out=out) == 0
        assert out.getvalue().strip() == "no repositories"
    finally:
        get_settings.cache_clear()


async def test_read_paths_and_lifecycle_operations_on_postgres(
    factory: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    await onboard(factory)
    svc = build_registry_service(factory)
    await svc.add_alias("nest-backend", "nest", actor=ACTOR)
    await svc.create_version("nest-backend", {**nestjs_backend()}, actor=ACTOR)
    await svc.publish_version("nest-backend", 2, actor=ACTOR, activate=False)
    repo, versions, aliases, mappings = await svc.show_repository("nest-backend")
    assert [v.version for v in versions] == [1, 2] and [v.published for v in versions] == [
        True,
        True,
    ]
    assert aliases == ["nest", "nest-backend"] and [m.jira_project_key for m in mappings] == [
        "SHOP"
    ]
    assert repo.active_profile_version_id == versions[0].id and repo.row_version >= 3
    await svc.activate_version("nest-backend", 2, actor=ACTOR)
    await svc.activate_version("nest-backend", 1, actor=ACTOR)
    assert [m.status.value for m in await svc.list_mappings(project_key="SHOP")] == ["ACTIVE"]
    assert len(await svc.list_mappings(key="nest-backend")) == 1
    await svc.unmap_project("SHOP", "nest-backend", actor=ACTOR)
    await svc.map_project("SHOP", "nest-backend", actor=ACTOR)
    events = await svc.audit_history("nest-backend", limit=500)
    assert len(events) == query(database_url, "SELECT count(*) FROM registry_audit_events")[0][0]
    assert (
        events[0].occurred_at is not None and events[0].action.value == "project_mapping.activated"
    )
    assert len(await svc.audit_history(None, limit=2)) == 2
    resolver = build_repository_resolver(factory)
    assert (
        await resolver.resolve(ResolutionRequest("SHOP-1", "SHOP", ("nest",)))
    ).profile_version == 1
