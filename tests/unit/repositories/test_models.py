import uuid
from dataclasses import replace

import pytest

from ai_code_change_agent.domain.repositories.errors import RegistryValidationError
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    AuditEvent,
    new_repository_findings,
    normalise_alias,
    safe_metadata,
    validate_actor,
    validate_project_key,
)
from tests.support.registry_fixtures import new_repo


def codes(**over: str) -> set[str]:
    return {f.code for f in new_repository_findings(replace(new_repo("shop-api"), **over))}


def test_valid_repository_has_no_findings_and_canonical_locator() -> None:
    new = new_repo("shop-api", owner="Acme", name="Shop-API")
    assert new_repository_findings(new) == []
    assert new.canonical_locator == "github:acme/shop-api"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("repository_key", "Shop", "key_invalid"),
        ("repository_key", "a", "key_invalid"),
        ("repository_key", "shop_api", "key_invalid"),
        ("display_name", "", "display_name_invalid"),
        ("display_name", "x\ny", "display_name_invalid"),
        ("git_provider", "GitHub", "provider_invalid"),
        ("git_provider", "git hub", "provider_invalid"),
        ("provider_repository_id", "a b", "provider_id_invalid"),
        ("owner", "../x", "owner_invalid"),
        ("owner", "..", "owner_invalid"),
        ("name", "repo.git", "name_invalid"),
        ("name", "a/b", "name_invalid"),
        ("default_branch", "main..x", "branch_invalid"),
        ("default_branch", "-main", "branch_invalid"),
        ("default_branch", "feature/", "branch_invalid"),
        ("default_branch", "a//b", "branch_invalid"),
        ("default_branch", "x.lock", "branch_invalid"),
    ],
)
def test_repository_field_validation(field: str, value: str, code: str) -> None:
    assert code in codes(**{field: value})


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/acme/shop-api.git",
        "git@github.com:acme/shop-api.git",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "github_pat_11AAAA",
        "secret:ghp_0123456789abcdefghijklmnop",
        "token=abc",
        "-----BEGIN PRIVATE KEY-----",
        "secret",
        "unknown-scheme:abc",
        "secret:" + "a" * 300,
        "",
    ],
)
def test_access_reference_must_be_opaque(ref: str) -> None:
    assert "access_reference_invalid" in codes(access_reference=ref)


@pytest.mark.parametrize(
    "ref", ["app-installation:98765", "secret:shop/api-token", "parameter:/ci/shop"]
)
def test_access_reference_accepts_opaque_pointers(ref: str) -> None:
    assert codes(access_reference=ref) == set()


def test_all_findings_are_reported_together() -> None:
    assert (
        len(codes(repository_key="X", owner="..", default_branch="-x", access_reference="http://x"))
        >= 4
    )


def test_alias_and_project_and_actor_validation() -> None:
    assert normalise_alias("  Shop-API ") == "shop-api"
    for bad in ("", "a", "has space", "UPPER_case", "../x", "http://x"):
        with pytest.raises(RegistryValidationError):
            normalise_alias(bad)
    assert validate_project_key("SHOP") == "SHOP"
    for bad in ("shop", "S", "SHOP-1", "SHOP KEY", ""):
        with pytest.raises(RegistryValidationError):
            validate_project_key(bad)
    assert validate_actor("cli:alice") == "cli:alice"
    for bad in ("alice", "cli:", "CLI:alice", "cli:a b"):
        with pytest.raises(RegistryValidationError):
            validate_actor(bad)


def test_safe_metadata_rejects_secrets_and_non_scalars() -> None:
    assert safe_metadata({"version": 3, "ok": True, "key": "shop"}) == {
        "version": 3,
        "ok": True,
        "key": "shop",
    }
    for bad in (
        {"x": "ghp_abcdefghijklmnop"},
        {"x": "a" * 201},
        {"x": ["list"]},
        {"Bad-Key": "v"},
        {"x": "password = hunter2"},
    ):
        with pytest.raises(ValueError):
            safe_metadata(bad)  # type: ignore[arg-type,unused-ignore]


def test_audit_event_defaults() -> None:
    e = AuditEvent(AuditAction.ALIAS_ADDED, "repository", uuid.uuid4(), "cli:a")
    assert e.metadata == {} and e.occurred_at is None
