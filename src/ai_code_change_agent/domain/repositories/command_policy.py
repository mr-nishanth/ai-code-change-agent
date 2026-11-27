"""Platform-owned command policy. Profiles *request* argv commands; this module decides.

Nothing here executes anything. Unknown toolchains, executables, flags or shapes fail closed.
Repository execution is Python only: "python" is the single toolchain family, a reviewed
platform setting, never repository data.
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
ALLOWED_ENV_NAMES = frozenset({"CI", "PYTHONHASHSEED", "TZ", "LANG"})
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}")
_DIGITS = re.compile(r"[0-9]{1,3}")


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
