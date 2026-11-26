"""Gate D.2: operator registration helper reading the identity from the GitHub App (read-only)."""

import asyncio
import io
from pathlib import Path

import pytest

from ai_code_change_agent.application.git_providers.ports import GitProviderRegistry
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.cli.main import main
from ai_code_change_agent.domain.repositories.errors import ProviderMismatchError
from ai_code_change_agent.domain.repositories.models import RepositoryStatus
from ai_code_change_agent.integrations.github.authentication import GitHubAppAuth
from ai_code_change_agent.integrations.github.credentials import (
    GitHubAppCredentials,
)
from ai_code_change_agent.integrations.github.repositories import (
    GitHubAppRepositoryClient,
)
from tests.support.fake_github import FakeGitHub, make_key
from tests.support.github_repos import FILES, commit
from tests.support.registry_memory import InMemoryRegistry

REF = "app-installation:22"


def rig(tmp_path: Path) -> tuple[FakeGitHub, GitHubAppRepositoryClient]:
    bare = tmp_path / "remote.git"
    commit(bare, FILES)
    key = make_key(tmp_path / "app.pem")
    hub = FakeGitHub(bare, public_key=key.public_key(), owner="acme",
                     name="shop_service", repo_id=1468634307)  # fmt: skip
    auth = GitHubAppAuth(GitHubAppCredentials(hub.app_id, hub.installation_id, key))
    return hub, GitHubAppRepositoryClient(auth=auth, transport=hub.transport())


async def test_the_client_reads_the_canonical_identity(tmp_path: Path) -> None:
    hub, client = rig(tmp_path)
    info = await client.describe_repository(owner="Acme", name="shop_service", access_reference=REF)
    assert (info.provider_repository_id, info.owner, info.name, info.default_branch) == (
        "1468634307", "acme", "shop_service", "main")  # fmt: skip
    assert [p for p, _ in hub.token_meta.values()] == [{"metadata": "read"}]
    assert [m for m, _ in hub.writes()] == ["POST"]  # only the token exchange


@pytest.mark.parametrize(
    ("setup", "kw", "code"),
    [
        (lambda h: None, {"access_reference": "app-installation:99"},
         "access_reference_not_this_installation"),
        (lambda h: None, {"name": "billing_service"}, "installation_access_denied"),
        (lambda h: setattr(h, "accessible", set()), {}, "installation_access_denied"),
        (lambda h: None, {"owner": "someone-else"}, "provider_identity_mismatch"),
        (lambda h: setattr(h, "archived", True), {}, "repository_archived"),
        (lambda h: None, {"owner": "a/b"}, "provider_identity_mismatch"),
    ],
)  # fmt: skip
async def test_the_client_refuses_anything_outside_the_installation(
    tmp_path: Path, setup: object, kw: dict[str, str], code: str
) -> None:
    hub, client = rig(tmp_path)
    setup(hub)  # type: ignore[operator]
    args = {"owner": "acme", "name": "shop_service", "access_reference": REF} | kw
    with pytest.raises(ProviderMismatchError) as err:
        await client.describe_repository(**args)
    assert str(err.value) == code


def cli(reg: InMemoryRegistry, client: object, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    providers = GitProviderRegistry({"github": client}) if client else None  # type: ignore[dict-item]
    code = main(
        list(argv),
        service_factory=lambda: (reg.service(providers=providers), RepositoryResolver(reg.uow)),
        out=out, err_out=err,
    )  # fmt: skip
    return code, out.getvalue(), err.getvalue()


BASE = ["repositories", "register", "--key", "shop-service", "--display-name", "Acme",
        "--access-ref", REF, "--no-profile"]  # fmt: skip


def test_register_from_provider_stores_a_disabled_repository_only(tmp_path: Path) -> None:
    _hub, client = rig(tmp_path)
    reg = InMemoryRegistry()
    code, out, err = cli(reg, client, *BASE, "--from-provider", "acme/shop_service")
    assert code == 0, err
    assert "status DISABLED" in out and "--enable" in out

    async def check() -> None:
        async with reg.uow() as uow:
            (repo,) = await uow.store.list_repositories()
            assert (
                repo.status is RepositoryStatus.DISABLED and repo.active_profile_version_id is None
            )
            assert (repo.provider_repository_id, repo.default_branch) == ("1468634307", "main")
            assert repo.canonical_locator == "github:acme/shop_service"
            assert await uow.store.list_mappings(repository_id=repo.id) == []

    asyncio.run(check())


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--from-provider", "acme/shop_service", "--provider-id", "1"],
         "arguments_conflict"),
        (["--from-provider", "not-a-pair"], "reference_invalid"),
        (["--from-provider", "a/b/c"], "reference_invalid"),
        (["--provider", "github"], "arguments_missing"),
    ],
)  # fmt: skip
def test_register_argument_errors(tmp_path: Path, extra: list[str], message: str) -> None:
    _hub, client = rig(tmp_path)
    reg = InMemoryRegistry()
    code, _out, err = cli(reg, client, *BASE, *extra)
    assert code == 1 and message in err


def test_register_from_provider_without_app_configuration_fails(tmp_path: Path) -> None:
    reg = InMemoryRegistry()
    code, _out, err = cli(reg, None, *BASE, "--from-provider", "acme/shop_service")
    assert code == 1 and "provider_client_unavailable" in err


def test_one_command_onboarding_derives_the_key_and_creates_the_standard_profile(
    tmp_path: Path,
) -> None:
    _hub, client = rig(tmp_path)
    reg = InMemoryRegistry()
    code, out, err = cli(
        reg, client, "repositories", "register", "--from-provider", "acme/shop_service",
        "--access-ref", REF, "--project", "shop", "--enable",
    )  # fmt: skip
    assert code == 0, err
    assert "registered shop-service" in out and "profile v1 published" in out
    assert "mapped to SHOP" in out and "ACTIVE" in out
