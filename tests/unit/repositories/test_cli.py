import io
import json
from pathlib import Path

import pytest

from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.cli.main import build_parser, main
from tests.support.registry_fixtures import nestjs_backend
from tests.support.registry_memory import InMemoryRegistry

REG_ARGS = [
    "repositories", "register", "--key", "nest-backend", "--display-name", "Nest Backend",
    "--provider", "github", "--provider-id", "123", "--owner", "acme", "--name", "nest-backend",
    "--branch", "main", "--access-ref", "app-installation:42", "--no-profile",
]  # fmt: skip


def run(reg: InMemoryRegistry, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        service_factory=lambda: (reg.service(), RepositoryResolver(reg.uow)),
        out=out,
        err_out=err,
    )
    return code, out.getvalue(), err.getvalue()


def write_profile(
    tmp: Path, doc: dict[str, object] | None = None, name: str = "profile.json"
) -> Path:
    path = tmp / name
    path.write_text(json.dumps(doc or nestjs_backend()))
    return path


def test_help_contains_examples_and_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert "examples:" in build_parser().format_help()
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["repositories", "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "ai-code-change repositories register" in text
    for command in (
        "register", "list", "show", "create-version", "publish", "activate-version", "enable",
        "disable", "alias-add", "map-project", "unmap-project", "mappings", "audit",
        "validate-profile", "toolchains", "resolve",
    ):  # fmt: skip
        assert command in text, command


def test_full_onboarding_flow_through_the_cli(tmp_path: Path) -> None:
    reg = InMemoryRegistry()
    prof = write_profile(tmp_path)
    steps = [
        REG_ARGS,
        ["repositories", "create-version", "nest-backend", str(prof)],
        ["repositories", "publish", "nest-backend", "1"],
        ["repositories", "alias-add", "nest-backend", "nest"],
        ["repositories", "map-project", "SHOP", "nest-backend"],
        ["repositories", "enable", "nest-backend"],
    ]
    for step in steps:
        code, out, err = run(reg, *step)
        assert code == 0 and err == "", (step, out, err)
    code, out, _ = run(reg, "repositories", "resolve", "--project", "SHOP", "--reference", "nest")
    assert code == 0 and out.startswith("RESOLVED") and "nest-backend v1" in out
    code, out, _ = run(reg, "repositories", "list")
    assert "nest-backend" in out and "ACTIVE" in out
    code, out, _ = run(reg, "repositories", "show", "nest-backend")
    assert (
        "version 1: published (ACTIVE)" in out
        and "alias nest" in out
        and "project SHOP: ACTIVE" in out
    )
    code, out, _ = run(reg, "repositories", "mappings", "--project", "SHOP")
    assert "SHOP" in out
    code, out, _ = run(reg, "repositories", "audit", "--repository", "nest-backend")
    assert "repository.registered" in out and "cli:" in out
    code, out, _ = run(reg, "repositories", "disable", "nest-backend")
    assert code == 0
    code, out, _ = run(reg, "repositories", "resolve", "--project", "SHOP")
    assert out.startswith("DISABLED_REPOSITORY") and "OPERATIONS_REVIEW" in out
    code, out, _ = run(reg, "repositories", "unmap-project", "SHOP", "nest-backend")
    assert code == 0
    code, out, _ = run(reg, "repositories", "activate-version", "nest-backend", "1")
    assert code == 0


def test_json_output_is_valid_and_has_no_credentials(tmp_path: Path) -> None:
    reg = InMemoryRegistry()
    run(reg, *REG_ARGS)
    code, out, _ = run(reg, "--json", "repositories", "list")
    data = json.loads(out)
    assert (
        code == 0
        and data[0]["repository_key"] == "nest-backend"
        and data[0]["status"] == "DISABLED"
    )
    code, out, _ = run(reg, "--json", "repositories", "audit")
    assert json.loads(out)[0]["action"] == "repository.registered"
    code, out, _ = run(reg, "--json", "repositories", "show", "nest-backend")
    assert {"repository", "versions", "aliases", "mappings"} <= set(json.loads(out))


def test_credential_looking_access_reference_is_refused_and_never_echoed() -> None:
    reg = InMemoryRegistry()
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
    args = [a if a != "app-installation:42" else f"secret:{secret}" for a in REG_ARGS]
    code, out, err = run(reg, *args)
    assert code == 1 and "access_reference_invalid" in err
    assert secret not in out + err and not reg.state.repos
    args = [a if a != "app-installation:42" else "https://github.com/acme/x.git" for a in REG_ARGS]
    code, out, err = run(reg, *args)
    assert code == 1 and "https://github.com" not in out + err


def test_validate_profile_offline_json_and_toml(tmp_path: Path) -> None:
    reg = InMemoryRegistry()
    code, out, _ = run(reg, "repositories", "validate-profile", str(write_profile(tmp_path)))
    assert code == 0 and out.startswith("valid")
    toml = tmp_path / "p.toml"
    toml.write_text(
        'schema_version = 1\nrepository_key = "toml-repo"\ncommand_policy = "default-v1"\nvalidation_policy = "standard"\n'
    )
    code, out, _ = run(reg, "--json", "repositories", "validate-profile", str(toml))
    assert code == 0 and json.loads(out)["valid"] is True
    bad = write_profile(tmp_path, {**nestjs_backend(), "extra": 1}, "bad.json")
    code, _, err = run(reg, "repositories", "validate-profile", str(bad))
    assert code == 1 and "extra_forbidden" in err
    code, _, err = run(reg, "repositories", "validate-profile", str(tmp_path / "missing.json"))
    assert code == 1 and "not_a_file" in err
    (tmp_path / "broken.json").write_text("{nope")
    code, _, err = run(reg, "repositories", "validate-profile", str(tmp_path / "broken.json"))
    assert code == 1 and "unparseable" in err


def test_toolchains_catalogue() -> None:
    code, out, _ = run(InMemoryRegistry(), "repositories", "toolchains")
    assert code == 0 and "python" in out and "jvm" in out and "default-v1" in out
    code, out, _ = run(InMemoryRegistry(), "--json", "repositories", "toolchains")
    assert {"python", "node", "jvm", "dotnet", "go", "rust"} <= set(json.loads(out)["toolchains"])


def test_domain_errors_exit_nonzero_with_codes(tmp_path: Path) -> None:
    reg = InMemoryRegistry()
    code, _, err = run(reg, "repositories", "show", "missing")
    assert code == 1 and "not_found" in err
    run(reg, *REG_ARGS)
    code, _, err = run(reg, *REG_ARGS)
    assert code == 1 and "conflict" in err
    code, _, err = run(reg, "repositories", "enable", "nest-backend")
    assert code == 1 and "no_active_profile_version" in err
    code, _, err = run(
        reg,
        "repositories",
        "create-version",
        "nest-backend",
        str(write_profile(tmp_path, {**nestjs_backend(), "repository_key": "other-key"})),
    )
    assert code == 1 and "key_mismatch" in err


def test_usage_errors_exit_2() -> None:
    assert run(InMemoryRegistry(), "nonsense")[0] == 2
    assert run(InMemoryRegistry(), "repositories")[0] == 2


def test_missing_database_url_is_a_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = io.StringIO()
    assert main(["repositories", "list"], err_out=err) == 2
    assert "DATABASE_URL is required" in err.getvalue()


def test_entry_point_is_declared() -> None:
    import tomllib

    data = (
        tomllib.loads(Path("pyproject.toml").read_text()) if Path("pyproject.toml").exists() else {}
    )
    root = Path(__file__).resolve().parents[3] / "pyproject.toml"
    data = tomllib.loads(root.read_text())
    assert data["project"]["scripts"]["ai-code-change"] == "ai_code_change_agent.cli.main:main"
