"""Platform-owned command policy. Profiles *request* argv commands; this module decides.

Nothing here executes anything. Unknown toolchains, executables, flags or shapes fail closed.
Adding a toolchain family is a reviewed edit to the catalog below, never repository data.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ai_code_change_agent.domain.repositories.catalog import MAX_COMMAND_TIMEOUT_CEILING
from ai_code_change_agent.domain.repositories.errors import Finding

POLICY_ID = "default-v1"
POLICY_VERSION = "1"

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_./=:,@+-]{1,200}")
_CWD = re.compile(r"\.|[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}")
_ENV_VALUE = re.compile(r"[A-Za-z0-9_.:/-]{0,100}")
ALLOWED_ENV_NAMES = frozenset({"CI", "NODE_ENV", "PYTHONHASHSEED", "TZ", "LANG"})
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}")
_GO_PKG = re.compile(r"\./\.\.\.|\./[A-Za-z0-9_][A-Za-z0-9_./-]{0,100}(/\.\.\.)?")
_DIGITS = re.compile(r"[0-9]{1,3}")
_PLAIN = re.compile(r"plain")


class CommandKind(StrEnum):
    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    FORMAT_CHECK = "format_check"
    INSTALL = "install"


KIND_CAPABILITY: Mapping[CommandKind, str] = {
    CommandKind.TEST: "test_run",
    CommandKind.BUILD: "build_run",
    CommandKind.LINT: "lint_run",
    CommandKind.FORMAT_CHECK: "format_check",
    CommandKind.INSTALL: "dependency_install",
}


@dataclass(frozen=True)
class CommandRule:
    """One permitted command shape: exact prefix, then only listed flags and bounded positionals."""

    kind: CommandKind
    prefix: tuple[str, ...]
    flags: frozenset[str] = frozenset()
    value_flags: Mapping[str, re.Pattern[str]] = field(default_factory=dict)
    positional: re.Pattern[str] | None = None
    max_positional: int = 0
    network: bool = False


@dataclass(frozen=True)
class ToolchainPolicy:
    family: str
    rules: tuple[CommandRule, ...]

    @property
    def executables(self) -> frozenset[str]:
        return frozenset(rule.prefix[0] for rule in self.rules)


def _rule(kind: CommandKind, *prefix: str, **kw: object) -> CommandRule:
    return CommandRule(kind=kind, prefix=prefix, **kw)  # type: ignore[arg-type]


K = CommandKind
_PY_FLAGS = frozenset({"-q", "-v", "-vv", "-x", "--strict-markers", "--no-header"})
_NODE_NET = {"network": True}

DEFAULT_TOOLCHAINS: tuple[ToolchainPolicy, ...] = (
    ToolchainPolicy(
        "python",
        (
            _rule(
                K.TEST,
                "pytest",
                flags=_PY_FLAGS,
                value_flags={"--maxfail": _DIGITS},
                positional=_PATH,
                max_positional=5,
            ),
            _rule(
                K.TEST,
                "python",
                "-m",
                "pytest",
                flags=_PY_FLAGS,
                value_flags={"--maxfail": _DIGITS},
                positional=_PATH,
                max_positional=5,
            ),
            _rule(
                K.LINT,
                "ruff",
                "check",
                flags=frozenset({"--no-fix", "--quiet"}),
                positional=_PATH,
                max_positional=5,
            ),
            _rule(K.FORMAT_CHECK, "ruff", "format", "--check", positional=_PATH, max_positional=5),
            _rule(
                K.LINT,
                "mypy",
                flags=frozenset({"--strict", "--ignore-missing-imports"}),
                positional=_PATH,
                max_positional=5,
            ),
        ),
    ),
    ToolchainPolicy(
        "node",
        (
            _rule(K.TEST, "npm", "test"),
            _rule(K.TEST, "npm", "run", "test"),
            _rule(K.LINT, "npm", "run", "lint"),
            _rule(K.LINT, "npm", "run", "typecheck"),
            _rule(K.BUILD, "npm", "run", "build"),
            _rule(K.FORMAT_CHECK, "npm", "run", "format:check"),
            _rule(K.INSTALL, "npm", "ci", "--ignore-scripts", **_NODE_NET),
            _rule(K.TEST, "yarn", "test"),
            _rule(K.LINT, "yarn", "lint"),
            _rule(K.BUILD, "yarn", "build"),
            _rule(K.TEST, "pnpm", "test"),
            _rule(K.LINT, "pnpm", "lint"),
            _rule(K.BUILD, "pnpm", "build"),
        ),
    ),
    ToolchainPolicy(
        "jvm",
        (
            _rule(
                K.TEST,
                "mvn",
                "test",
                flags=frozenset({"-q", "-B", "--batch-mode", "--no-transfer-progress"}),
            ),
            _rule(
                K.LINT,
                "mvn",
                "verify",
                flags=frozenset({"-q", "-B", "--batch-mode", "--no-transfer-progress"}),
            ),
            _rule(
                K.BUILD,
                "mvn",
                "compile",
                flags=frozenset({"-q", "-B", "--batch-mode", "--no-transfer-progress"}),
            ),
            _rule(
                K.TEST,
                "gradle",
                "test",
                flags=frozenset({"--no-daemon"}),
                value_flags={"--console": _PLAIN},
            ),
            _rule(
                K.LINT,
                "gradle",
                "check",
                flags=frozenset({"--no-daemon"}),
                value_flags={"--console": _PLAIN},
            ),
            _rule(
                K.BUILD,
                "gradle",
                "build",
                flags=frozenset({"--no-daemon"}),
                value_flags={"--console": _PLAIN},
            ),
        ),
    ),
    ToolchainPolicy(
        "dotnet",
        (
            _rule(K.TEST, "dotnet", "test", flags=frozenset({"--no-build", "--nologo"})),
            _rule(K.BUILD, "dotnet", "build", flags=frozenset({"--nologo"})),
            _rule(K.FORMAT_CHECK, "dotnet", "format", "--verify-no-changes"),
        ),
    ),
    ToolchainPolicy(
        "go",
        (
            _rule(
                K.TEST,
                "go",
                "test",
                flags=frozenset({"-v", "-race"}),
                positional=_GO_PKG,
                max_positional=3,
            ),
            _rule(K.BUILD, "go", "build", positional=_GO_PKG, max_positional=3),
            _rule(K.LINT, "go", "vet", positional=_GO_PKG, max_positional=3),
        ),
    ),
    ToolchainPolicy(
        "rust",
        (
            _rule(K.TEST, "cargo", "test", flags=frozenset({"--quiet", "--locked", "--offline"})),
            _rule(K.BUILD, "cargo", "build", flags=frozenset({"--quiet", "--locked", "--offline"})),
            _rule(K.LINT, "cargo", "clippy", flags=frozenset({"--quiet", "--locked", "--offline"})),
            _rule(K.FORMAT_CHECK, "cargo", "fmt", "--check"),
        ),
    ),
)


class CommandPolicy:
    def __init__(
        self,
        toolchains: tuple[ToolchainPolicy, ...] = DEFAULT_TOOLCHAINS,
        *,
        policy_id: str = POLICY_ID,
        version: str = POLICY_VERSION,
    ) -> None:
        self.policy_id = policy_id
        self.version = version
        self._toolchains = {t.family: t for t in toolchains}

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(sorted(self._toolchains))

    def describe(self) -> dict[str, tuple[str, ...]]:
        return {f: tuple(sorted(t.executables)) for f, t in sorted(self._toolchains.items())}

    def has_family(self, family: str) -> bool:
        return family in self._toolchains

    def check(
        self,
        *,
        field_path: str,
        family: str,
        kind: CommandKind,
        argv: tuple[str, ...],
        cwd: str,
        timeout_seconds: int,
        env: Mapping[str, str],
        network_requested: bool,
    ) -> list[Finding]:
        out: list[Finding] = []

        def bad(sub: str, code: str, message: str) -> None:
            out.append(Finding(f"{field_path}.{sub}", code, message))

        toolchain = self._toolchains.get(family)
        if toolchain is None:
            bad("toolchain", "unknown_toolchain", "toolchain family is not in the platform policy")
            return out
        if not 1 <= timeout_seconds <= MAX_COMMAND_TIMEOUT_CEILING:
            bad("timeout_seconds", "timeout_out_of_range", "outside the platform ceiling")
        if not _CWD.fullmatch(cwd) or ".." in cwd.split("/"):
            bad("cwd", "cwd_invalid", "must be a relative path inside the workspace")
        for name, value in env.items():
            if name not in ALLOWED_ENV_NAMES:
                bad(
                    "env",
                    "env_not_allowed",
                    "environment variable is not on the platform allow-list",
                )
            elif not _ENV_VALUE.fullmatch(value):
                bad("env", "env_value_invalid", "environment value contains disallowed characters")
        if not argv:
            bad("argv", "argv_empty", "argv must not be empty")
            return out
        for token in argv:
            segments = token.split("/")
            if (
                not _SAFE_TOKEN.fullmatch(token)
                or ".." in segments
                or token.startswith("/")
                or any(seg.startswith("/") for seg in token.split("="))
                or "://" in token
            ):
                bad("argv", "argv_token_unsafe", "token contains disallowed characters or paths")
                return out
        if argv[0] not in toolchain.executables:
            bad("argv", "executable_not_allowed", "executable is not permitted for this toolchain")
            return out
        rule = next((r for r in toolchain.rules if argv[: len(r.prefix)] == r.prefix), None)
        if rule is None:
            bad("argv", "command_shape_not_allowed", "subcommand is not permitted")
            return out
        if rule.kind is not kind:
            bad("kind", "kind_mismatch", f"this command is a '{rule.kind}' command")
        if network_requested and not rule.network:
            bad("network", "network_not_allowed", "this command may not use the network")
        if rule.network and not network_requested:
            bad(
                "network", "network_required", "this command needs package-registry access declared"
            )
        positionals = 0
        for token in argv[len(rule.prefix) :]:
            if token in rule.flags:
                continue
            name, sep, value = token.partition("=")
            if sep and name in rule.value_flags and rule.value_flags[name].fullmatch(value):
                continue
            if (
                not token.startswith("-")
                and rule.positional is not None
                and rule.positional.fullmatch(token)
            ):
                positionals += 1
                if positionals > rule.max_positional:
                    bad("argv", "too_many_arguments", "too many positional arguments")
                continue
            bad("argv", "argument_not_allowed", "argument is not permitted for this command")
        return out
