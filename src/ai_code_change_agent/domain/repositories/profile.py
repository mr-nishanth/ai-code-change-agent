"""RepositoryProfile schema v1 (validated, immutable configuration data) and cross-checks."""

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_code_change_agent.domain.repositories.catalog import (
    CAPABILITIES,
    COMMAND_POLICY_IDS,
    MAX_DIFF_BYTES_CEILING,
    MAX_FILES_CHANGED_CEILING,
    MAX_INDEX_FILE_BYTES_CEILING,
    MAX_RUNTIME_SECONDS_CEILING,
    VALIDATION_POLICIES,
)
from ai_code_change_agent.domain.repositories.command_policy import (
    KIND_CAPABILITY,
    CommandKind,
    CommandPolicy,
)
from ai_code_change_agent.domain.repositories.errors import Finding

PROFILE_SCHEMA_VERSION = 1
_ID = r"^[a-z][a-z0-9_-]{1,39}$"
_LABEL = r"^[A-Za-z0-9+#._-]{1,40}$"
_GLOB = re.compile(r"[A-Za-z0-9_.*/{},-]{1,120}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CommandSpec(_Strict):
    id: str = Field(pattern=_ID)
    kind: CommandKind
    toolchain: str = Field(pattern=_ID)
    argv: tuple[str, ...] = Field(min_length=1, max_length=16)
    cwd: str = Field(default=".", max_length=151)
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    env: dict[str, str] = Field(default_factory=dict, max_length=5)
    network: Literal["none", "package_registry"] = "none"


class Indexing(_Strict):
    include: tuple[str, ...] = Field(default=(), max_length=50)
    exclude: tuple[str, ...] = Field(default=(), max_length=50)
    max_file_bytes: int = Field(default=1_048_576, ge=1024, le=MAX_INDEX_FILE_BYTES_CEILING)


class Limits(_Strict):
    max_files_changed: int = Field(default=50, ge=1, le=MAX_FILES_CHANGED_CEILING)
    max_diff_bytes: int = Field(default=1_048_576, ge=1024, le=MAX_DIFF_BYTES_CEILING)
    max_runtime_seconds: int = Field(default=3600, ge=60, le=MAX_RUNTIME_SECONDS_CEILING)


class RepositoryProfile(_Strict):
    schema_version: Literal[1]
    repository_key: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    languages: tuple[str, ...] = Field(default=(), max_length=20)
    frameworks: tuple[str, ...] = Field(default=(), max_length=20)
    toolchains: tuple[str, ...] = Field(default=(), max_length=10)
    capabilities: tuple[str, ...] = Field(default=(), max_length=10)
    command_policy: str = Field(max_length=40)
    commands: tuple[CommandSpec, ...] = Field(default=(), max_length=40)
    validation_policy: str = Field(max_length=40)
    indexing: Indexing = Field(default_factory=Indexing)
    limits: Limits = Field(default_factory=Limits)
    execution_tier: Literal["small", "medium", "large"] = "small"


def canonical_hash(content: dict[str, Any]) -> str:
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def profile_findings(profile: RepositoryProfile, policy: CommandPolicy) -> list[Finding]:
    """Cross-field and platform-policy checks. Returns every finding, not just the first."""
    out: list[Finding] = []
    if (
        profile.command_policy not in COMMAND_POLICY_IDS
        or profile.command_policy != policy.policy_id
    ):
        out.append(Finding("command_policy", "unknown_policy", "not an approved command policy id"))
    if profile.validation_policy not in VALIDATION_POLICIES:
        out.append(
            Finding("validation_policy", "unknown_policy", "not an approved validation policy")
        )
    for label_field in ("languages", "frameworks"):
        for value in getattr(profile, label_field):
            if not re.fullmatch(_LABEL, value):
                out.append(Finding(label_field, "label_invalid", "unsafe label"))
    for cap in profile.capabilities:
        if cap not in CAPABILITIES:
            out.append(Finding("capabilities", "unknown_capability", "not a platform capability"))
    if len(set(profile.capabilities)) != len(profile.capabilities):
        out.append(Finding("capabilities", "duplicate", "duplicate capability"))
    for fam in profile.toolchains:
        if not policy.has_family(fam):
            out.append(
                Finding(
                    "toolchains",
                    "unknown_toolchain",
                    "toolchain family is not in the platform policy",
                )
            )
    for glob_field in ("include", "exclude"):
        for pattern in getattr(profile.indexing, glob_field):
            if (
                not _GLOB.fullmatch(pattern)
                or ".." in pattern.split("/")
                or pattern.startswith("/")
            ):
                out.append(Finding(f"indexing.{glob_field}", "glob_invalid", "unsafe glob"))
    seen: set[str] = set()
    for i, cmd in enumerate(profile.commands):
        path = f"commands.{i}"
        if cmd.id in seen:
            out.append(Finding(f"{path}.id", "duplicate", "duplicate command id"))
        seen.add(cmd.id)
        if cmd.toolchain not in profile.toolchains:
            out.append(
                Finding(f"{path}.toolchain", "toolchain_not_declared", "declare it in toolchains")
            )
        needed = KIND_CAPABILITY[cmd.kind]
        if needed not in profile.capabilities:
            out.append(
                Finding(
                    f"{path}.kind", "capability_not_granted", f"profile does not select '{needed}'"
                )
            )
        out.extend(
            policy.check(
                field_path=path,
                family=cmd.toolchain,
                kind=cmd.kind,
                argv=cmd.argv,
                cwd=cmd.cwd,
                timeout_seconds=cmd.timeout_seconds,
                env=cmd.env,
                network_requested=cmd.network == "package_registry",
            )
        )
    return out
