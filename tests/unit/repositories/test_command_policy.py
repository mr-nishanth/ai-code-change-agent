import pytest

from ai_code_change_agent.domain.repositories.command_policy import (
    ALLOWED_ENV_NAMES,
    CommandKind,
    CommandPolicy,
    CommandRule,
    ToolchainPolicy,
)

POLICY = CommandPolicy()
K = CommandKind


def check(
    family: str,
    kind: CommandKind,
    *argv: str,
    cwd: str = ".",
    timeout: int = 60,
    env: dict[str, str] | None = None,
    network: bool = False,
) -> set[str]:
    return {
        f.code
        for f in POLICY.check(
            field_path="c",
            family=family,
            kind=kind,
            argv=tuple(argv),
            cwd=cwd,
            timeout_seconds=timeout,
            env=env or {},
            network_requested=network,
        )
    }


ALLOWED = [
    ("python", K.TEST, ("pytest", "-q", "tests")),
    ("python", K.TEST, ("python", "-m", "pytest", "--maxfail=3", "tests/unit")),
    ("python", K.LINT, ("ruff", "check", "src")),
    ("python", K.FORMAT_CHECK, ("ruff", "format", "--check", "src")),
    ("python", K.LINT, ("mypy", "--strict", "src")),
    ("node", K.TEST, ("npm", "test")),
    ("node", K.LINT, ("npm", "run", "lint")),
    ("node", K.BUILD, ("npm", "run", "build")),
    ("node", K.FORMAT_CHECK, ("npm", "run", "format:check")),
    ("node", K.TEST, ("yarn", "test")),
    ("node", K.BUILD, ("pnpm", "build")),
    ("jvm", K.TEST, ("mvn", "test", "-B")),
    ("jvm", K.BUILD, ("gradle", "build", "--no-daemon", "--console=plain")),
    ("dotnet", K.TEST, ("dotnet", "test", "--nologo")),
    ("dotnet", K.FORMAT_CHECK, ("dotnet", "format", "--verify-no-changes")),
    ("go", K.TEST, ("go", "test", "-race", "./...")),
    ("go", K.LINT, ("go", "vet", "./pkg/api/...")),
    ("rust", K.TEST, ("cargo", "test", "--locked")),
    ("rust", K.FORMAT_CHECK, ("cargo", "fmt", "--check")),
]


@pytest.mark.parametrize(("family", "kind", "argv"), ALLOWED)
def test_allowed_commands(family: str, kind: CommandKind, argv: tuple[str, ...]) -> None:
    assert check(family, kind, *argv) == set()


def test_install_needs_declared_network() -> None:
    argv = ("npm", "ci", "--ignore-scripts")
    assert check("node", K.INSTALL, *argv, network=True) == set()
    assert check("node", K.INSTALL, *argv) == {"network_required"}


DENIED = [
    ("python", ("bash", "-c", "pytest"), "executable_not_allowed"),
    ("python", ("sh", "-c", "id"), "executable_not_allowed"),
    ("python", ("env", "pytest"), "executable_not_allowed"),
    ("python", ("sudo", "pytest"), "executable_not_allowed"),
    ("python", ("curl", "x"), "executable_not_allowed"),
    ("python", ("pip", "install", "x"), "executable_not_allowed"),
    ("python", ("uv", "run", "x"), "executable_not_allowed"),
    ("python", ("python", "-c", "print"), "command_shape_not_allowed"),
    ("python", ("python", "script.py"), "command_shape_not_allowed"),
    ("python", ("pytest", "; rm -rf /"), "argv_token_unsafe"),
    ("python", ("pytest", "$(whoami)"), "argv_token_unsafe"),
    ("python", ("pytest", "`id`"), "argv_token_unsafe"),
    ("python", ("pytest", "a|b"), "argv_token_unsafe"),
    ("python", ("pytest", "a&&b"), "argv_token_unsafe"),
    ("python", ("pytest", "../outside"), "argv_token_unsafe"),
    ("python", ("pytest", "/etc/passwd"), "argv_token_unsafe"),
    ("python", ("pytest", "--rootdir=/tmp"), "argv_token_unsafe"),
    ("python", ("pytest", "http://evil.invalid/x"), "argv_token_unsafe"),
    ("python", ("pytest", "--unknown-flag"), "argument_not_allowed"),
    ("python", ("pytest", "--maxfail=abc"), "argument_not_allowed"),
    ("python", ("pytest", "-p", "evil"), "argument_not_allowed"),
    ("python", ("pytest", "a", "b", "c", "d", "e", "f"), "too_many_arguments"),
    ("python", ("ruff", "format", "src"), "command_shape_not_allowed"),
    ("python", ("ruff", "check", "--fix", "src"), "argument_not_allowed"),
    ("node", ("npm", "install"), "command_shape_not_allowed"),
    ("node", ("npm", "run", "publish"), "command_shape_not_allowed"),
    ("node", ("npx", "x"), "executable_not_allowed"),
    ("node", ("node", "x.js"), "executable_not_allowed"),
    ("node", ("npm", "test", "--", "x"), "argument_not_allowed"),
    ("jvm", ("./mvnw", "test"), "executable_not_allowed"),
    ("jvm", ("mvn", "deploy"), "command_shape_not_allowed"),
    ("go", ("go", "run", "."), "command_shape_not_allowed"),
    ("go", ("go", "test", "../x"), "argv_token_unsafe"),
    ("rust", ("cargo", "install", "x"), "command_shape_not_allowed"),
    ("dotnet", ("dotnet", "publish"), "command_shape_not_allowed"),
]


@pytest.mark.parametrize(("family", "argv", "code"), DENIED)
def test_denied_commands(family: str, argv: tuple[str, ...], code: str) -> None:
    assert code in check(family, K.TEST, *argv)


def test_unknown_toolchain_fails_closed() -> None:
    assert check("cobol", K.TEST, "pytest") == {"unknown_toolchain"}
    assert not POLICY.has_family("cobol")


def test_cross_family_executables_are_rejected() -> None:
    assert "executable_not_allowed" in check("python", K.TEST, "npm", "test")
    assert "executable_not_allowed" in check("node", K.TEST, "pytest")


def test_kind_mismatch_and_network_misuse() -> None:
    assert "kind_mismatch" in check("python", K.BUILD, "pytest", "-q")
    assert "network_not_allowed" in check("python", K.TEST, "pytest", network=True)


@pytest.mark.parametrize("cwd", ["..", "../x", "/abs", "a/../b", "a b", "", "x" * 200])
def test_bad_cwd(cwd: str) -> None:
    assert "cwd_invalid" in check("python", K.TEST, "pytest", cwd=cwd)


def test_cwd_ok() -> None:
    assert check("python", K.TEST, "pytest", cwd="services/api") == set()


@pytest.mark.parametrize("timeout", [0, -1, 3601])
def test_timeout_bounds(timeout: int) -> None:
    assert "timeout_out_of_range" in check("python", K.TEST, "pytest", timeout=timeout)


def test_environment_is_allow_listed() -> None:
    assert check("python", K.TEST, "pytest", env={"CI": "true"}) == set()
    for name in ("LD_PRELOAD", "PATH", "PYTHONPATH", "HOME", "NODE_OPTIONS"):
        assert name not in ALLOWED_ENV_NAMES
        assert "env_not_allowed" in check("python", K.TEST, "pytest", env={name: "x"})
    assert "env_value_invalid" in check("python", K.TEST, "pytest", env={"CI": "a;b"})


def test_empty_argv() -> None:
    assert "argv_empty" in check("python", K.TEST)


def test_policy_is_extensible_by_catalog_not_by_profile() -> None:
    custom = CommandPolicy(
        (ToolchainPolicy("ruby", (CommandRule(K.TEST, ("rspec",)),)),),
        policy_id="custom",
        version="9",
    )
    assert custom.families == ("ruby",)
    assert custom.describe() == {"ruby": ("rspec",)}
    assert not CommandPolicy().has_family("ruby")
