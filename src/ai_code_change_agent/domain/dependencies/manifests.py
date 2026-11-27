"""Safe repository dependency manifest discovery, parsing and category separation.

Untrusted repository files (``uv.lock``, ``pyproject.toml``, ``requirements*.txt``) are read
purely in Python without executing shell commands, build backends or package code. All disk reads
stay confined inside ``repo_root`` with symlink, FIFO and size protections.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from packaging.markers import InvalidMarker, Marker
from packaging.utils import canonicalize_name

from ai_code_change_agent.domain.dependencies.policy import (
    DependencyFailureCode,
    DependencyPolicyError,
    DependencyRequest,
    TargetEnvironment,
    default_target_environment,
    merge_two_requests,
    parse_manifest_requirement,
    specifiers_compatible,
    validate_python_requirement,
    validate_requests,
    wheel_identity,
)

MAX_MANIFEST_BYTES = 1_048_576
MAX_TOTAL_MANIFEST_BYTES = 5_242_880
MAX_INCLUDE_DEPTH = 5
MAX_INCLUDE_FILES = 20
_MAX_RELPATH_LENGTH = 300

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SHA256_PREFIX_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_WHEEL_TAGS_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?-[A-Za-z0-9.!+_]+"
    r"(?:-[0-9][A-Za-z0-9._]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$"
)

_TEST_DEV_GROUP_NAMES = frozenset(
    {"dev", "test", "tests", "testing", "lint", "typing", "typecheck", "check", "qa", "ci"}
)
_REQUIREMENTS_RUNTIME_FILES = ("requirements.txt",)
# older layouts: a requirements/ directory (used only when there is no root requirements.txt)
_REQUIREMENTS_DIR_RUNTIME_FILES = (
    "requirements/base.txt",
    "requirements/common.txt",
    "requirements/prod.txt",
    "requirements/production.txt",
    "requirements/requirements.txt",
)
_REQUIREMENTS_DEV_FILES = (
    "requirements-test.txt",
    "requirements-tests.txt",
    "requirements-testing.txt",
    "requirements-dev.txt",
    "requirements_dev.txt",
    "requirements_test.txt",
    "test-requirements.txt",
    "dev-requirements.txt",
    "requirements/test.txt",
    "requirements/tests.txt",
    "requirements/testing.txt",
    "requirements/dev.txt",
    "requirements/development.txt",
)
# `-e .` / `.` install the repository itself; tests already run from its source tree
_SELF_INSTALL_LINES = frozenset({"-e .", "-e ./", "--editable .", "--editable=.", ".", "./"})
_LEGACY_UNSUPPORTED_FILES = (
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
)
_SAFE_BUILD_BACKENDS = frozenset(
    {
        "setuptools.build_meta",
        "setuptools.build_meta:__legacy__",
        "hatchling.build",
        "flit_core.buildapi",
        "poetry.core.masonry.api",
        "pdm.backend",
        "uv_build",
    }
)
_FORBIDDEN_PIP_FLAGS = frozenset(
    {
        "-i",
        "--index-url",
        "--extra-index-url",
        "--no-index",
        "-f",
        "--find-links",
        "--trusted-host",
        "-e",
        "--editable",
        "-c",
        "--constraint",
        "--pre",
        "--no-binary",
        "--only-binary",
        "--prefer-binary",
        "--use-deprecated",
        "--config-settings",
        "-C",
        "--global-option",
        "--install-option",
        "--build-option",
        "--no-deps",
        "--user",
        "--target",
        "--prefix",
    }
)
_FORBIDDEN_UV_KEYS = frozenset(
    {
        "index-url",
        "extra-index-url",
        "default-index",
        "index",
        "index-strategy",
        "find-links",
        "allow-insecure-host",
        "trusted-host",
        "no-index",
        "constraint",
        "constraints",
        "constraint-dependencies",
        "build-constraint",
        "build-constraints",
        "build-constraint-dependencies",
        "override-dependencies",
    }
)
_UNSAFE_UV_SOURCE_KEYS = frozenset(
    {
        "git",
        "url",
        "path",
        "index",
        "workspace",
        "editable",
        "rev",
        "tag",
        "branch",
        "subdirectory",
    }
)
_SAFE_UV_SOURCE_KEYS = frozenset({"marker", "extra"})


class DependencyCategory(StrEnum):
    RUNTIME = "runtime"
    TEST_DEV = "test_dev"
    OPTIONAL = "optional"


@dataclass(frozen=True)
class CategorizedDependencies:
    runtime: tuple[DependencyRequest, ...] = ()
    test_dev: tuple[DependencyRequest, ...] = ()
    optional: Mapping[str, tuple[DependencyRequest, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ManifestDiscoveryResult:
    source_kind: Literal["uv.lock", "pyproject.toml", "requirements.txt", "none"] = "none"
    sources: tuple[str, ...] = ()
    requires_python: str | None = None
    categories: CategorizedDependencies = field(default_factory=CategorizedDependencies)
    build_system: tuple[str, ...] = ()
    provisioned: tuple[DependencyRequest, ...] = ()


class _RepoReader(Protocol):
    def exists_regular(self, relpath: str) -> bool: ...
    def read_text(self, relpath: str) -> str: ...


def validate_manifest_relpath(raw: str, *, base_dir: str = "") -> str:
    """Validate and normalise a repository-relative manifest path, preventing escapes."""
    text = raw.strip()
    if (
        not text
        or len(text) > _MAX_RELPATH_LENGTH
        or "://" in text
        or "\\" in text
        or _CONTROL_RE.search(text)
        or text.startswith(("/", "~"))
        or _WINDOWS_DRIVE_RE.match(text)
    ):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="unsafe_manifest_path"
        )
    combined = f"{base_dir}/{text}" if base_dir else text
    stack: list[str] = []
    for part in combined.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="manifest_path_escape"
                )
            stack.pop()
            continue
        if part == ".git" or part.startswith(".env"):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="unsafe_manifest_path"
            )
        stack.append(part)
    if not stack:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="empty_manifest_path"
        )
    return "/".join(stack)


class _SafeRepoReader:
    """Read regular UTF-8 files strictly inside ``repo_root`` using ``O_NOFOLLOW``."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root
        self._total_bytes = 0
        try:
            root_fd = os.open(self._root, _DIR_FLAGS)
        except OSError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_repo_root"
            ) from None
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_repo_root"
                )
        finally:
            os.close(root_fd)

    def _open_parent(self, relpath: str) -> tuple[int, str]:
        clean = validate_manifest_relpath(relpath)
        parts = clean.split("/")
        try:
            fd = os.open(self._root, _DIR_FLAGS)
        except OSError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_repo_root"
            ) from None
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    raise
                except OSError:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="unsafe_manifest_path"
                    ) from None
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        return fd, parts[-1]

    def exists_regular(self, relpath: str) -> bool:
        try:
            dir_fd, leaf = self._open_parent(relpath)
        except FileNotFoundError:
            return False
        try:
            st = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="unsafe_manifest_path"
            ) from None
        finally:
            os.close(dir_fd)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="non_regular_manifest_file"
            )
        return True

    def read_text(self, relpath: str) -> str:
        try:
            dir_fd, leaf = self._open_parent(relpath)
        except FileNotFoundError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"missing_include:{relpath}"
            ) from None
        try:
            try:
                fd = os.open(leaf, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dir_fd)
            except FileNotFoundError:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"missing_include:{relpath}"
                ) from None
            except OSError:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="unsafe_manifest_path"
                ) from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="non_regular_manifest_file"
                    )
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(dir_fd)
        if len(data) > MAX_MANIFEST_BYTES:
            raise DependencyPolicyError(
                DependencyFailureCode.LIMIT_EXCEEDED, detail=f"manifest_too_large:{relpath}"
            )
        self._total_bytes += len(data)
        if self._total_bytes > MAX_TOTAL_MANIFEST_BYTES:
            raise DependencyPolicyError(
                DependencyFailureCode.LIMIT_EXCEEDED, detail="total_manifest_bytes_exceeded"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_utf8:{relpath}"
            ) from None


class _MappingRepoReader:
    """In-memory ``_RepoReader`` over verified baseline file bytes."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files: dict[str, bytes] = {}
        for raw_path, content in files.items():
            # Keep normal repo files accessible; unsafe paths simply won't match valid names.
            norm = raw_path
            while norm.startswith("./"):
                norm = norm[2:]
            self._files[norm] = content
        self._total_bytes = 0

    def exists_regular(self, relpath: str) -> bool:
        clean = validate_manifest_relpath(relpath)
        return clean in self._files

    def read_text(self, relpath: str) -> str:
        clean = validate_manifest_relpath(relpath)
        data = self._files.get(clean)
        if data is None:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"missing_include:{clean}"
            )
        if len(data) > MAX_MANIFEST_BYTES:
            raise DependencyPolicyError(
                DependencyFailureCode.LIMIT_EXCEEDED, detail=f"manifest_too_large:{clean}"
            )
        self._total_bytes += len(data)
        if self._total_bytes > MAX_TOTAL_MANIFEST_BYTES:
            raise DependencyPolicyError(
                DependencyFailureCode.LIMIT_EXCEEDED, detail="total_manifest_bytes_exceeded"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_utf8:{clean}"
            ) from None


def _merge_list(requests: Sequence[DependencyRequest]) -> tuple[DependencyRequest, ...]:
    merged: dict[str, DependencyRequest] = {}
    for req in requests:
        existing = merged.get(req.name)
        merged[req.name] = req if existing is None else merge_two_requests(existing, req)
    return tuple(merged[name] for name in sorted(merged))


def _evaluate_marker(marker_expr: str, env: TargetEnvironment) -> bool:
    try:
        marker = Marker(marker_expr)
        return bool(marker.evaluate(env.as_marker_env()))
    except (InvalidMarker, ValueError):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_marker"
        ) from None


def _is_manifest_pyproject(data: dict[str, Any]) -> bool:
    if any(k in data for k in ("project", "dependency-groups", "build-system")):
        return True
    tool = data.get("tool")
    return isinstance(tool, dict) and any(k in tool for k in ("poetry", "uv"))


def _validate_uv_tool_section(tool: dict[str, Any], env: TargetEnvironment) -> None:
    uv_cfg = tool.get("uv")
    if uv_cfg is None:
        return
    if not isinstance(uv_cfg, dict):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_tool_uv"
        )
    for forbidden in _FORBIDDEN_UV_KEYS:
        if forbidden in uv_cfg:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"forbidden_uv_key:{forbidden}"
            )
    pip_cfg = uv_cfg.get("pip")
    if pip_cfg is not None:
        if not isinstance(pip_cfg, dict):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_tool_uv_pip"
            )
        for forbidden in _FORBIDDEN_UV_KEYS:
            if forbidden in pip_cfg:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"forbidden_uv_pip_key:{forbidden}",
                )
    sources = uv_cfg.get("sources")
    if sources is not None:
        if not isinstance(sources, dict):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_sources"
            )
        for pkg_name, entry in sources.items():
            entries = entry if isinstance(entry, list) else [entry]
            for item in entries:
                if not isinstance(item, dict):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID,
                        detail=f"invalid_uv_source:{pkg_name}",
                    )
                if any(k in _UNSAFE_UV_SOURCE_KEYS or k not in _SAFE_UV_SOURCE_KEYS for k in item):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID,
                        detail=f"unsafe_uv_source:{pkg_name}",
                    )
    for env_key in ("environments", "required-environments"):
        markers = uv_cfg.get(env_key)
        if markers is not None:
            if not isinstance(markers, list) or not all(isinstance(m, str) for m in markers):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_uv_{env_key}"
                )
            if markers and not any(_evaluate_marker(m, env) for m in markers):
                raise DependencyPolicyError(
                    DependencyFailureCode.PLATFORM_UNSUPPORTED, detail=env_key
                )


def _validate_build_system(data: dict[str, Any], env: TargetEnvironment) -> tuple[str, ...]:
    build_sys = data.get("build-system")
    if build_sys is None:
        return ()
    if not isinstance(build_sys, dict):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_build_system"
        )
    if "backend-path" in build_sys:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="backend_path_forbidden"
        )
    backend = build_sys.get("build-backend")
    if backend is not None and (
        not isinstance(backend, str) or backend not in _SAFE_BUILD_BACKENDS
    ):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="unsupported_build_backend"
        )
    requires = build_sys.get("requires", [])
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_build_requires"
        )
    parsed: list[str] = []
    for item in requires:
        req = parse_manifest_requirement(item, target_env=env)
        if req is not None:
            parsed.append(req.requirement())
    return tuple(parsed)


def _parse_pyproject_data(
    data: dict[str, Any],
    env: TargetEnvironment,
    *,
    max_packages: int,
    sources: tuple[str, ...] = ("pyproject.toml",),
) -> tuple[ManifestDiscoveryResult, str | None]:
    tool = data.get("tool")
    if tool is not None and not isinstance(tool, dict):
        raise DependencyPolicyError(DependencyFailureCode.MANIFEST_INVALID, detail="invalid_tool")
    if isinstance(tool, dict):
        if "poetry" in tool:
            if "project" not in data:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="poetry_without_pep621"
                )
            poetry_cfg = tool["poetry"]
            if not isinstance(poetry_cfg, dict) or any(
                k in poetry_cfg for k in ("source", "dependencies", "dev-dependencies", "group")
            ):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail="unsupported_poetry_manifest_config",
                )
        _validate_uv_tool_section(tool, env)

    build_system = _validate_build_system(data, env)

    project = data.get("project", {})
    if not isinstance(project, dict):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_project"
        )
    raw_name = project.get("name")
    if raw_name is not None and not isinstance(raw_name, str):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_project_name"
        )
    project_name = str(canonicalize_name(raw_name)) if raw_name else None

    requires_python_raw = project.get("requires-python")
    if requires_python_raw is not None and not isinstance(requires_python_raw, str):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_requires_python"
        )
    validate_python_requirement(requires_python_raw, target_env=env)

    raw_deps = project.get("dependencies", [])
    if not isinstance(raw_deps, list) or not all(isinstance(item, str) for item in raw_deps):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_project_dependencies"
        )
    runtime_list: list[DependencyRequest] = []
    for item in raw_deps:
        parsed = parse_manifest_requirement(item, target_env=env)
        if parsed is not None and parsed.name != project_name:
            runtime_list.append(parsed)

    raw_opt = project.get("optional-dependencies", {})
    if not isinstance(raw_opt, dict):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_optional_dependencies"
        )
    opt_norm_map: dict[str, tuple[str, list[str]]] = {}
    for group_name, entries in raw_opt.items():
        if (
            not isinstance(group_name, str)
            or not isinstance(entries, list)
            or not all(isinstance(e, str) for e in entries)
        ):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_optional_group"
            )
        opt_norm_map[str(canonicalize_name(group_name))] = (group_name, entries)

    def resolve_optional_group(norm_group: str, stack: frozenset[str]) -> list[DependencyRequest]:
        if norm_group in stack:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"optional_cycle:{norm_group}"
            )
        if norm_group not in opt_norm_map:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unknown_extra:{norm_group}"
            )
        raw_group, items = opt_norm_map[norm_group]
        out: list[DependencyRequest] = []
        next_stack = stack | {norm_group}
        active_extras = frozenset({norm_group, raw_group})
        for item in items:
            parsed = parse_manifest_requirement(item, target_env=env, active_extras=active_extras)
            if parsed is None:
                continue
            if project_name is not None and parsed.name == project_name:
                if parsed.extras:
                    for extra_ref in parsed.extras:
                        out.extend(resolve_optional_group(extra_ref, next_stack))
                continue
            out.append(parsed)
        return out

    test_dev_list: list[DependencyRequest] = []
    optional_dict: dict[str, tuple[DependencyRequest, ...]] = {}
    for norm_group, (raw_group, _) in opt_norm_map.items():
        resolved = resolve_optional_group(norm_group, frozenset())
        if raw_group.lower() in _TEST_DEV_GROUP_NAMES:
            test_dev_list.extend(resolved)
        else:
            optional_dict[raw_group] = _merge_list(resolved)

    raw_groups = data.get("dependency-groups", {})
    if not isinstance(raw_groups, dict):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_dependency_groups"
        )
    groups_norm_map: dict[str, tuple[str, list[Any]]] = {}
    for group_name, entries in raw_groups.items():
        if not isinstance(group_name, str) or not isinstance(entries, list):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_dependency_group"
            )
        groups_norm_map[str(canonicalize_name(group_name))] = (group_name, entries)

    def resolve_dep_group(norm_group: str, stack: frozenset[str]) -> list[DependencyRequest]:
        if norm_group in stack:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"group_cycle:{norm_group}"
            )
        if norm_group not in groups_norm_map:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unknown_group:{norm_group}"
            )
        _, entries = groups_norm_map[norm_group]
        out: list[DependencyRequest] = []
        next_stack = stack | {norm_group}
        for item in entries:
            if isinstance(item, str):
                parsed = parse_manifest_requirement(item, target_env=env)
                if parsed is None:
                    continue
                if project_name is not None and parsed.name == project_name:
                    for extra_ref in parsed.extras:
                        out.extend(resolve_optional_group(extra_ref, frozenset()))
                    continue
                out.append(parsed)
            elif isinstance(item, dict):
                if set(item.keys()) != {"include-group"} or not isinstance(
                    item["include-group"], str
                ):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID,
                        detail="invalid_dependency_group_include",
                    )
                target_norm = str(canonicalize_name(item["include-group"]))
                out.extend(resolve_dep_group(target_norm, next_stack))
            else:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_dependency_group_item"
                )
        return out

    for norm_group, (raw_group, _) in groups_norm_map.items():
        resolved = resolve_dep_group(norm_group, frozenset())
        if raw_group.lower() in _TEST_DEV_GROUP_NAMES:
            test_dev_list.extend(resolved)
        else:
            existing = list(optional_dict.get(raw_group, ()))
            optional_dict[raw_group] = _merge_list([*existing, *resolved])

    runtime = _merge_list(runtime_list)
    test_dev = _merge_list(test_dev_list)
    provisioned = _merge_list([*runtime, *test_dev])
    if len(provisioned) > max_packages:
        raise DependencyPolicyError(DependencyFailureCode.LIMIT_EXCEEDED)

    result = ManifestDiscoveryResult(
        source_kind="pyproject.toml",
        sources=sources,
        requires_python=requires_python_raw,
        categories=CategorizedDependencies(
            runtime=runtime, test_dev=test_dev, optional=optional_dict
        ),
        build_system=build_system,
        provisioned=provisioned,
    )
    return result, project_name


def _validate_https_url(url: Any, *, detail: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise DependencyPolicyError(DependencyFailureCode.MANIFEST_INVALID, detail=detail)
    parsed = urlsplit(url)
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise DependencyPolicyError(DependencyFailureCode.MANIFEST_INVALID, detail=detail)
    return url


def _wheel_matches_env(filename: str, env: TargetEnvironment) -> tuple[bool, bool]:
    """Return ``(matches_platform, matches_python)`` for a wheel filename."""
    wheel_identity(filename)
    match = _WHEEL_TAGS_RE.fullmatch(filename)
    if match is None:
        raise DependencyPolicyError(DependencyFailureCode.WHEEL_UNAVAILABLE)
    py_parts = match.group("py").split(".")
    abi = match.group("abi")
    plat_parts = match.group("plat").split(".")

    is_any = "any" in plat_parts
    is_linux = is_any or any(p.startswith(("linux_", "manylinux", "musllinux")) for p in plat_parts)
    is_arch = is_any or any(p.endswith(f"_{env.platform_machine}") for p in plat_parts)
    matches_platform = is_linux and is_arch

    ver_parts = env.python_version.split(".")
    if len(ver_parts) < 2 or not (ver_parts[0].isdigit() and ver_parts[1].isdigit()):
        raise DependencyPolicyError(
            DependencyFailureCode.PYTHON_UNSUPPORTED, detail=env.python_version
        )
    major = int(ver_parts[0])
    minor = int(ver_parts[1])
    cp_tag = f"cp{major}{minor}"
    py_tag = f"py{major}{minor}"
    py_major_tag = f"py{major}"
    cp_prefix = f"cp{major}"

    matches_python = (
        cp_tag in py_parts
        or py_tag in py_parts
        or py_major_tag in py_parts
        or (
            abi == "abi3"
            and any(
                p.startswith(cp_prefix)
                and p[len(cp_prefix) :].isdigit()
                and int(p[len(cp_prefix) :]) <= minor
                for p in py_parts
            )
        )
    )
    return matches_platform, matches_python


def _parse_uv_lock(
    reader: _RepoReader,
    env: TargetEnvironment,
    *,
    max_packages: int,
    expected_index_url: str | None,
    pyproject_result: ManifestDiscoveryResult | None,
    pyproject_name: str | None,
) -> ManifestDiscoveryResult:
    try:
        data = tomllib.loads(reader.read_text("uv.lock"))
    except tomllib.TOMLDecodeError:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_toml"
        ) from None

    version = data.get("version")
    if isinstance(version, bool) or version != 1:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="unsupported_uv_lock_version"
        )

    req_py = data.get("requires-python")
    if req_py is not None and not isinstance(req_py, str):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_requires_python"
        )
    validate_python_requirement(req_py, target_env=env)

    top_markers = data.get("resolution-markers")
    if top_markers is not None:
        if not isinstance(top_markers, list) or not all(isinstance(m, str) for m in top_markers):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_resolution_markers"
            )
        if top_markers and not any(_evaluate_marker(m, env) for m in top_markers):
            raise DependencyPolicyError(
                DependencyFailureCode.PLATFORM_UNSUPPORTED, detail="uv_lock_resolution_markers"
            )

    raw_packages = data.get("package")
    if not isinstance(raw_packages, list) or not all(isinstance(p, dict) for p in raw_packages):
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_packages"
        )

    by_name: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for pkg in raw_packages:
        name = pkg.get("name")
        pkg_ver = pkg.get("version")
        if not isinstance(name, str) or not isinstance(pkg_ver, str):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_package_entry"
            )
        norm = str(canonicalize_name(name))
        by_name.setdefault(norm, []).append(pkg)
        src = pkg.get("source")
        if isinstance(src, dict) and (
            src in ({"editable": "."}, {"virtual": "."})
            or (
                pyproject_name is not None
                and norm == pyproject_name
                and ("editable" in src or "virtual" in src)
            )
        ):
            roots.append(pkg)

    if len(roots) != 1:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="missing_or_ambiguous_uv_lock_root"
        )
    root = roots[0]
    root_name = str(canonicalize_name(root["name"]))

    def select_package(edge: dict[str, Any]) -> dict[str, Any]:
        edge_name = edge.get("name")
        if not isinstance(edge_name, str):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_edge"
            )
        norm = str(canonicalize_name(edge_name))
        candidates = by_name.get(norm, [])
        if not candidates:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unresolved_lock_package:{norm}"
            )
        edge_ver = edge.get("version")
        if edge_ver is not None:
            if not isinstance(edge_ver, str):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_edge_version"
                )
            candidates = [c for c in candidates if c.get("version") == edge_ver]
            if not candidates:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"missing_lock_version:{norm}=={edge_ver}",
                )
        matched: list[dict[str, Any]] = []
        had_platform_markers = False
        for cand in candidates:
            res_markers = cand.get("resolution-markers")
            if res_markers is None:
                matched.append(cand)
                continue
            if not isinstance(res_markers, list) or not all(
                isinstance(m, str) for m in res_markers
            ):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"invalid_resolution_markers:{norm}",
                )
            had_platform_markers = True
            if not res_markers or any(_evaluate_marker(m, env) for m in res_markers):
                matched.append(cand)
        if not matched:
            if had_platform_markers:
                raise DependencyPolicyError(
                    DependencyFailureCode.PLATFORM_UNSUPPORTED, detail=f"resolution_markers:{norm}"
                )
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unresolved_lock_package:{norm}"
            )
        if len(matched) != 1:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"ambiguous_lock_package:{norm}"
            )
        return matched[0]

    def validate_package(
        pkg: dict[str, Any], incoming_extras: tuple[str, ...]
    ) -> DependencyRequest:
        norm = str(canonicalize_name(pkg["name"]))
        ver = str(pkg["version"])
        src = pkg.get("source")
        if not isinstance(src, dict) or set(src.keys()) != {"registry"}:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unsafe_lock_source:{norm}"
            )
        reg_url = _validate_https_url(src["registry"], detail=f"unsafe_registry_url:{norm}")
        if expected_index_url is not None and reg_url.rstrip("/") != expected_index_url.rstrip("/"):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"unexpected_registry:{norm}"
            )

        wheels = pkg.get("wheels", [])
        if not isinstance(wheels, list):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_wheels:{norm}"
            )
        if not wheels:
            raise DependencyPolicyError(
                DependencyFailureCode.WHEEL_UNAVAILABLE, detail=f"sdist_only:{norm}"
            )

        platform_wheels: list[tuple[str, bool]] = []
        for wheel in wheels:
            if not isinstance(wheel, dict):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_wheel_entry:{norm}"
                )
            w_url = _validate_https_url(wheel.get("url"), detail=f"unsafe_wheel_url:{norm}")
            w_hash_raw = wheel.get("hash")
            if not isinstance(w_hash_raw, str):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_wheel_hash:{norm}"
                )
            hash_match = _SHA256_PREFIX_RE.fullmatch(w_hash_raw.strip().lower())
            if hash_match is None:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_wheel_hash:{norm}"
                )
            filename = urlsplit(w_url).path.rsplit("/", 1)[-1]
            matches_plat, matches_py = _wheel_matches_env(filename, env)
            if matches_plat:
                platform_wheels.append((hash_match.group(1), matches_py))

        if not platform_wheels:
            raise DependencyPolicyError(
                DependencyFailureCode.PLATFORM_UNSUPPORTED, detail=f"no_linux_wheel:{norm}"
            )
        compatible_hashes = tuple(sorted({h for h, py_ok in platform_wheels if py_ok}))
        if not compatible_hashes:
            py_tag = env.python_version.replace(".", "")
            raise DependencyPolicyError(
                DependencyFailureCode.PYTHON_UNSUPPORTED, detail=f"no_py{py_tag}_wheel:{norm}"
            )
        return DependencyRequest(
            name=norm,
            specifiers=(f"=={ver}",),
            extras=tuple(sorted(set(incoming_extras))),
            hashes=compatible_hashes,
        )

    def collect_subgraph(seed_edges: Sequence[Any]) -> tuple[DependencyRequest, ...]:
        if not isinstance(seed_edges, list):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_edges"
            )
        queue: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        for edge in seed_edges:
            if not isinstance(edge, dict):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_edge"
                )
            marker = edge.get("marker")
            if marker is not None:
                if not isinstance(marker, str):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="invalid_edge_marker"
                    )
                if not _evaluate_marker(marker, env):
                    continue
            raw_extras = edge.get("extra", [])
            if not isinstance(raw_extras, list) or not all(
                isinstance(ex, str) for ex in raw_extras
            ):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_edge_extra"
                )
            extras = tuple(sorted(str(canonicalize_name(ex)) for ex in raw_extras))
            target = select_package(edge)
            if str(canonicalize_name(target["name"])) == root_name:
                continue
            queue.append((target, extras))

        visited_extras: set[tuple[str, str, str]] = set()
        collected: list[DependencyRequest] = []
        while queue:
            pkg, req_extras = queue.pop(0)
            norm = str(canonicalize_name(pkg["name"]))
            ver = str(pkg["version"])
            base_key = (norm, ver, "")
            new_extras = [ex for ex in req_extras if (norm, ver, ex) not in visited_extras]
            if base_key in visited_extras and not new_extras:
                continue
            visited_extras.add(base_key)
            for ex in new_extras:
                visited_extras.add((norm, ver, ex))

            collected.append(validate_package(pkg, tuple(new_extras)))

            next_edges: list[Any] = []
            deps = pkg.get("dependencies", [])
            if not isinstance(deps, list):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_deps:{norm}"
                )
            next_edges.extend(deps)

            pkg_opt = pkg.get("optional-dependencies", {})
            if not isinstance(pkg_opt, dict):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_pkg_opt:{norm}"
                )
            opt_lookup = {str(canonicalize_name(k)): v for k, v in pkg_opt.items()}
            for ex in new_extras:
                extra_edges = opt_lookup.get(ex, [])
                if not isinstance(extra_edges, list):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_pkg_opt:{norm}"
                    )
                next_edges.extend(extra_edges)

            for child_edge in next_edges:
                if not isinstance(child_edge, dict):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="invalid_uv_lock_edge"
                    )
                marker = child_edge.get("marker")
                if marker is not None:
                    if not isinstance(marker, str):
                        raise DependencyPolicyError(
                            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_edge_marker"
                        )
                    if not _evaluate_marker(marker, env):
                        continue
                child_extras_raw = child_edge.get("extra", [])
                if not isinstance(child_extras_raw, list) or not all(
                    isinstance(ex, str) for ex in child_extras_raw
                ):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="invalid_edge_extra"
                    )
                child_extras = tuple(sorted(str(canonicalize_name(ex)) for ex in child_extras_raw))
                child_pkg = select_package(child_edge)
                if str(canonicalize_name(child_pkg["name"])) == root_name:
                    continue
                queue.append((child_pkg, child_extras))

        return _merge_list(collected)

    runtime = collect_subgraph(root.get("dependencies", []))

    test_dev_seeds: list[Any] = []
    optional_dict: dict[str, tuple[DependencyRequest, ...]] = {}
    for section_name in ("dev-dependencies", "optional-dependencies"):
        section = root.get(section_name, {})
        if not isinstance(section, dict):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"invalid_root_{section_name}"
            )
        for group_name, edges in section.items():
            if not isinstance(group_name, str) or not isinstance(edges, list):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"invalid_root_group:{group_name}",
                )
            if group_name.lower() in _TEST_DEV_GROUP_NAMES:
                test_dev_seeds.extend(edges)
            else:
                existing = list(optional_dict.get(group_name, ()))
                optional_dict[group_name] = _merge_list([*existing, *collect_subgraph(edges)])

    test_dev = collect_subgraph(test_dev_seeds)
    provisioned = _merge_list([*runtime, *test_dev])

    if pyproject_result is not None:
        locked_by_name = {r.name: r for r in provisioned}
        for declared in pyproject_result.provisioned:
            locked = locked_by_name.get(declared.name)
            if locked is None:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"stale_uv_lock:{declared.name}",
                )
            if not specifiers_compatible(declared.specifiers, locked.specifiers):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail="stale_uv_lock_version",
                )

    if len(provisioned) > max_packages:
        raise DependencyPolicyError(DependencyFailureCode.LIMIT_EXCEEDED)

    sources = ("uv.lock", "pyproject.toml") if pyproject_result is not None else ("uv.lock",)
    effective_req_py = req_py or (pyproject_result.requires_python if pyproject_result else None)
    build_sys = pyproject_result.build_system if pyproject_result is not None else ()
    return ManifestDiscoveryResult(
        source_kind="uv.lock",
        sources=sources,
        requires_python=effective_req_py,
        categories=CategorizedDependencies(
            runtime=runtime, test_dev=test_dev, optional=optional_dict
        ),
        build_system=build_sys,
        provisioned=provisioned,
    )


def _strip_unquoted_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote is not None:
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
        elif ch == "#":
            return line[:idx].rstrip()
    return line.rstrip()


def _logical_lines(text: str) -> list[str]:
    joined: list[str] = []
    buf = ""
    for raw_line in text.splitlines():
        if not buf and raw_line.lstrip().startswith("#"):
            continue
        clean_line = _strip_unquoted_comment(raw_line)
        if clean_line.endswith("\\"):
            buf += clean_line[:-1] + " "
            continue
        if buf:
            joined.append(buf + clean_line)
            buf = ""
        elif clean_line:
            joined.append(clean_line)
    if buf:
        joined.append(buf)
    return joined


def _split_requirement_options(line: str) -> tuple[str, tuple[str, ...]]:
    """Separate trailing ``--hash=sha256:...`` options while preserving marker quotes."""
    hashes: list[str] = []
    out_parts: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if escaped:
            out_parts.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote is not None:
            out_parts.append(ch)
            escaped = True
            i += 1
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            out_parts.append(ch)
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            out_parts.append(ch)
            i += 1
            continue
        if ch == "-" and (i == 0 or line[i - 1].isspace()):
            rest = line[i:]
            token_match = re.match(r"^(-\S+)(?:\s+(\S+))?", rest)
            assert token_match is not None  # noqa: S101
            flag = token_match.group(1)
            if flag.startswith("--hash="):
                digest_spec = flag.removeprefix("--hash=").strip().lower()
                m = _SHA256_PREFIX_RE.fullmatch(digest_spec)
                if m is None:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="unsupported_hash_algorithm"
                    )
                hashes.append(m.group(1))
                i += len(flag)
                continue
            if flag == "--hash":
                next_val = token_match.group(2)
                if next_val is None:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="missing_hash_value"
                    )
                m = _SHA256_PREFIX_RE.fullmatch(next_val.strip().lower())
                if m is None:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="unsupported_hash_algorithm"
                    )
                hashes.append(m.group(1))
                i += token_match.end(2)
                continue
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail=f"forbidden_pip_flag:{flag}"
            )
        out_parts.append(ch)
        i += 1
    if quote is not None:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="unterminated_quote"
        )
    return "".join(out_parts).strip(), tuple(hashes)


def _parse_requirements_file(
    reader: _RepoReader,
    relpath: str,
    env: TargetEnvironment,
    *,
    depth: int,
    stack: frozenset[str],
    seen_files: list[str],
    parsed_cache: dict[str, list[DependencyRequest]],
    include_ops: list[int],
    constraints: list[DependencyRequest] | None = None,
    index_url: str | None = None,
) -> list[DependencyRequest]:
    """Parse one requirements file. `-r` includes are followed; `-c` constraint files (inside
    the repository) are collected into *constraints*: they narrow versions of packages requested
    elsewhere and never add packages. `-i/--index-url` is accepted only when it names the
    configured index; any other index option is refused."""
    clean = validate_manifest_relpath(relpath)
    if clean in stack:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail=f"requirements_cycle:{clean}"
        )
    if depth > MAX_INCLUDE_DEPTH:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="requirements_include_depth_exceeded"
        )
    include_ops[0] += 1
    if include_ops[0] > MAX_INCLUDE_FILES * 5:
        raise DependencyPolicyError(
            DependencyFailureCode.LIMIT_EXCEEDED, detail="requirements_include_count_exceeded"
        )
    if clean in parsed_cache:
        return list(parsed_cache[clean])
    if len(seen_files) >= MAX_INCLUDE_FILES:
        raise DependencyPolicyError(
            DependencyFailureCode.LIMIT_EXCEEDED, detail="requirements_include_count_exceeded"
        )
    seen_files.append(clean)
    base_dir = str(PurePosixPath(clean).parent)
    if base_dir == ".":
        base_dir = ""

    next_stack = stack | {clean}
    results: list[DependencyRequest] = []
    for raw_line in _logical_lines(reader.read_text(clean)):
        line = _strip_unquoted_comment(raw_line).strip()
        if not line:
            continue
        if " ".join(line.split()) in _SELF_INSTALL_LINES:
            continue
        if line.startswith("-"):
            try:
                tokens = shlex.split(line)
            except ValueError:
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID, detail="invalid_requirements_syntax"
                ) from None
            if not tokens:
                continue
            head = tokens[0]
            if head in {"-r", "--requirement"}:
                if len(tokens) != 2:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="invalid_include_directive"
                    )
                target = validate_manifest_relpath(tokens[1], base_dir=base_dir)
                results.extend(
                    _parse_requirements_file(
                        reader,
                        target,
                        env,
                        depth=depth + 1,
                        stack=next_stack,
                        seen_files=seen_files,
                        parsed_cache=parsed_cache,
                        include_ops=include_ops,
                        constraints=constraints,
                        index_url=index_url,
                    )
                )
                continue
            if head.startswith("--requirement="):
                if len(tokens) != 1:
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="invalid_include_directive"
                    )
                target = validate_manifest_relpath(
                    head.removeprefix("--requirement="), base_dir=base_dir
                )
                results.extend(
                    _parse_requirements_file(
                        reader,
                        target,
                        env,
                        depth=depth + 1,
                        stack=next_stack,
                        seen_files=seen_files,
                        parsed_cache=parsed_cache,
                        include_ops=include_ops,
                        constraints=constraints,
                        index_url=index_url,
                    )
                )
                continue
            flag_name = head.split("=", 1)[0]
            value = head.split("=", 1)[1] if "=" in head else (tokens[1] if len(tokens) > 1 else "")
            if flag_name in {"-c", "--constraint"} and constraints is not None:
                if not value or len(tokens) > (1 if "=" in head else 2):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID,
                        detail="invalid_constraint_directive",
                    )
                target = validate_manifest_relpath(value, base_dir=base_dir)
                for item in _parse_requirements_file(
                    reader,
                    target,
                    env,
                    depth=depth + 1,
                    stack=next_stack,
                    seen_files=seen_files,
                    parsed_cache=parsed_cache,
                    include_ops=include_ops,
                    constraints=constraints,
                    index_url=index_url,
                ):
                    constraints.append(item)
                continue
            if flag_name in {"-i", "--index-url"} and index_url is not None:
                if value.rstrip("/") != index_url.rstrip("/") or len(tokens) > (
                    1 if "=" in head else 2
                ):
                    raise DependencyPolicyError(
                        DependencyFailureCode.MANIFEST_INVALID, detail="index_not_allowed"
                    )
                continue
            if flag_name in _FORBIDDEN_PIP_FLAGS or head.startswith("-"):
                raise DependencyPolicyError(
                    DependencyFailureCode.MANIFEST_INVALID,
                    detail=f"forbidden_pip_flag:{flag_name}",
                )

        req_text, hashes = _split_requirement_options(line)
        if not req_text:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="hash_without_requirement"
            )
        parsed = parse_manifest_requirement(req_text, target_env=env, hashes=hashes)
        if parsed is not None:
            results.append(parsed)
    parsed_cache[clean] = list(results)
    return results


def _apply_constraints(
    requests: tuple[DependencyRequest, ...], constraints: Sequence[DependencyRequest]
) -> tuple[DependencyRequest, ...]:
    """Narrow each request by the constraints on the same package (pip `-c` semantics: a
    constraint never adds a package). Incompatible constraints are a conflict."""
    if not constraints:
        return requests
    by_name: dict[str, list[DependencyRequest]] = {}
    for c in constraints:
        by_name.setdefault(c.name, []).append(c)
    out: list[DependencyRequest] = []
    for r in requests:
        merged = r
        for c in by_name.get(r.name, []):
            merged = merge_two_requests(merged, replace(c, extras=()))
        out.append(merged)
    return tuple(out)


def _discover(
    reader: _RepoReader,
    *,
    max_packages: int,
    target_env: TargetEnvironment | None,
    expected_index_url: str | None,
) -> ManifestDiscoveryResult:
    env = target_env or default_target_environment()

    pyproject_result: ManifestDiscoveryResult | None = None
    pyproject_name: str | None = None
    if reader.exists_regular("pyproject.toml"):
        try:
            pyproject_data = tomllib.loads(reader.read_text("pyproject.toml"))
        except tomllib.TOMLDecodeError:
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID, detail="invalid_pyproject_toml"
            ) from None
        if _is_manifest_pyproject(pyproject_data):
            pyproject_result, pyproject_name = _parse_pyproject_data(
                pyproject_data, env, max_packages=max_packages
            )

    if reader.exists_regular("uv.lock"):
        return _parse_uv_lock(
            reader,
            env,
            max_packages=max_packages,
            expected_index_url=expected_index_url,
            pyproject_result=pyproject_result,
            pyproject_name=pyproject_name,
        )

    if pyproject_result is not None:
        return pyproject_result

    seen_files: list[str] = []
    parsed_cache: dict[str, list[DependencyRequest]] = {}
    include_ops = [0]
    runtime_list: list[DependencyRequest] = []
    test_dev_list: list[DependencyRequest] = []
    constraints: list[DependencyRequest] = []
    index_url = expected_index_url or "https://pypi.org/simple"

    runtime_files: tuple[str, ...] = _REQUIREMENTS_RUNTIME_FILES
    if not any(reader.exists_regular(f) for f in runtime_files):
        runtime_files = _REQUIREMENTS_DIR_RUNTIME_FILES
    for filename in runtime_files:
        if reader.exists_regular(filename):
            runtime_list.extend(
                _parse_requirements_file(
                    reader,
                    filename,
                    env,
                    depth=0,
                    stack=frozenset(),
                    seen_files=seen_files,
                    parsed_cache=parsed_cache,
                    include_ops=include_ops,
                    constraints=constraints,
                    index_url=index_url,
                )
            )
    for filename in _REQUIREMENTS_DEV_FILES:
        if reader.exists_regular(filename):
            test_dev_list.extend(
                _parse_requirements_file(
                    reader,
                    filename,
                    env,
                    depth=0,
                    stack=frozenset(),
                    seen_files=seen_files,
                    parsed_cache=parsed_cache,
                    include_ops=include_ops,
                    constraints=constraints,
                    index_url=index_url,
                )
            )

    if seen_files:
        runtime = _apply_constraints(_merge_list(runtime_list), constraints)
        test_dev = _apply_constraints(_merge_list(test_dev_list), constraints)
        provisioned = _merge_list([*runtime, *test_dev])
        if len(provisioned) > max_packages:
            raise DependencyPolicyError(DependencyFailureCode.LIMIT_EXCEEDED)
        return ManifestDiscoveryResult(
            source_kind="requirements.txt",
            sources=tuple(seen_files),
            requires_python=None,
            categories=CategorizedDependencies(runtime=runtime, test_dev=test_dev),
            build_system=(),
            provisioned=provisioned,
        )

    for legacy in _LEGACY_UNSUPPORTED_FILES:
        if reader.exists_regular(legacy):
            raise DependencyPolicyError(
                DependencyFailureCode.MANIFEST_INVALID,
                detail=f"unsupported_legacy_manifest:{legacy}",
            )

    return ManifestDiscoveryResult(source_kind="none")


def discover_repository_dependencies(
    repo_root: Path,
    *,
    max_packages: int = 60,
    target_env: TargetEnvironment | None = None,
    expected_index_url: str | None = None,
) -> ManifestDiscoveryResult:
    """Safely discover and parse repository dependency manifests inside ``repo_root``."""
    return _discover(
        _SafeRepoReader(repo_root),
        max_packages=max_packages,
        target_env=target_env,
        expected_index_url=expected_index_url,
    )


def discover_dependencies_from_files(
    files: Mapping[str, bytes],
    *,
    max_packages: int = 60,
    target_env: TargetEnvironment | None = None,
    expected_index_url: str | None = None,
) -> ManifestDiscoveryResult:
    """Safely discover and parse dependency manifests from an in-memory file map."""
    return _discover(
        _MappingRepoReader(files),
        max_packages=max_packages,
        target_env=target_env,
        expected_index_url=expected_index_url,
    )


def merge_dependency_requests(
    discovered: ManifestDiscoveryResult,
    plan_requests: Sequence[str],
    *,
    max_packages: int,
) -> tuple[DependencyRequest, ...]:
    """Deterministically merge discovered repository dependencies with plan requests."""
    validated_plan = validate_requests(plan_requests, max_packages=max_packages)
    merged: dict[str, DependencyRequest] = {req.name: req for req in discovered.provisioned}
    for req in validated_plan:
        existing = merged.get(req.name)
        merged[req.name] = req if existing is None else merge_two_requests(existing, req)
    if len(merged) > max_packages:
        raise DependencyPolicyError(DependencyFailureCode.LIMIT_EXCEEDED)
    return tuple(merged[name] for name in sorted(merged))
