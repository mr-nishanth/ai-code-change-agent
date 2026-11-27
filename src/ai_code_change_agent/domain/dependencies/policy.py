"""Python dependency requests and manifest requirements, validated by the platform.

Agent implementation plans may only *request* plain packages (a PEP 508 name with optional
version specifiers; URLs, file paths, VCS references, extras and environment markers are refused).
Repository manifests may declare extras, PEP 508 markers and sha256 wheel hashes, which are
evaluated against the execution target environment before anything is resolved. Trusted
infrastructure resolves, downloads (wheels only) and hash-locks them
(docs/decisions/ADR-010-dynamic-dependency-provisioning.md).
"""

import platform
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from packaging.markers import InvalidMarker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, Specifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

MAX_REQUEST_LENGTH = 200
MAX_MANIFEST_REQUEST_LENGTH = 512
_REQUEST = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)\s*(.*)")
_SPECIFIER = re.compile(r"(?:===|==|~=|!=|<=|>=|<|>)[0-9][A-Za-z0-9.*+!_-]{0,40}")
_WHEEL = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-(?P<version>[A-Za-z0-9.!+_]+)"
    r"(?:-[0-9][A-Za-z0-9._]*)?-[A-Za-z0-9._]+-[A-Za-z0-9._]+-[A-Za-z0-9._]+\.whl"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CREDENTIAL_RE = re.compile(
    r"(?:://[^/@\s]+@|[A-Za-z0-9_.+-]+:[^/@\s]+@|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)",
    re.IGNORECASE,
)
_UNSAFE_URL_OR_PATH_RE = re.compile(r"(?:://|^(?:git\+|hg\+|svn\+|bzr\+|[.~]?/))", re.IGNORECASE)
_ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".egg")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DependencyFailureCode(StrEnum):
    REQUEST_INVALID = "dependency_request_invalid"
    CONFLICT = "dependency_conflict"
    LIMIT_EXCEEDED = "dependency_limit_exceeded"
    WHEEL_UNAVAILABLE = "dependency_wheel_unavailable"
    PYTHON_UNSUPPORTED = "dependency_python_unsupported"
    PREPARATION_TIMEOUT = "dependency_preparation_timeout"
    PREPARATION_FAILED = "dependency_preparation_failed"
    INSTALL_FAILED = "dependency_install_failed"
    IMAGE_UNAVAILABLE = "dependency_image_unavailable"
    BUNDLE_CORRUPTED = "dependency_bundle_corrupted"
    BUNDLE_CHANGED = "dependency_bundle_changed"
    PREPARATION_UNAVAILABLE = "dependency_preparation_unavailable"
    DIR_NOT_ALLOWED = "dependency_dir_not_allowed"
    MANIFEST_INVALID = "dependency_manifest_invalid"
    PLATFORM_UNSUPPORTED = "dependency_platform_unsupported"
    HASH_MISMATCH = "dependency_hash_mismatch"
    METADATA_UNAVAILABLE = "dependency_metadata_unavailable"


class DependencyPolicyError(Exception):
    """A request, manifest or bundle violates the dependency policy. The message is the code."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


DependencyError = DependencyPolicyError


@dataclass(frozen=True)
class TargetEnvironment:
    """Execution sandbox target environment for PEP 508 marker and compatibility checks."""

    python_version: str = "3.12"
    python_full_version: str = "3.12.16"
    sys_platform: str = "linux"
    os_name: str = "posix"
    platform_system: str = "Linux"
    platform_machine: str = "x86_64"

    def as_marker_env(self, *, extra: str = "") -> dict[str, str]:
        env = {
            "python_version": self.python_version,
            "python_full_version": self.python_full_version,
            "sys_platform": self.sys_platform,
            "os_name": self.os_name,
            "platform_system": self.platform_system,
            "platform_machine": self.platform_machine,
            "platform_release": "",
            "platform_version": "",
            "platform_python_implementation": "CPython",
            "implementation_name": "cpython",
            "implementation_version": self.python_full_version,
        }
        if extra:
            env["extra"] = extra
        return env


def default_target_environment() -> TargetEnvironment:
    machine = platform.machine().lower()
    arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    return TargetEnvironment(platform_machine=arch)


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class DependencyRequest:
    name: str  # normalised
    specifiers: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    hashes: tuple[str, ...] = ()

    def requirement(self) -> str:
        extra_part = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extra_part}{','.join(self.specifiers)}"

    def cache_token(self) -> str:
        base = self.requirement()
        if not self.hashes:
            return base
        return f"{base}#sha256:{','.join(sorted(self.hashes))}"


def parse_request(raw: str) -> DependencyRequest:
    text = raw.strip()
    match = _REQUEST.fullmatch(text) if 0 < len(text) <= MAX_REQUEST_LENGTH else None
    if match is None or match.group(1).lower().endswith(_ARCHIVE_SUFFIXES):
        raise DependencyPolicyError(DependencyFailureCode.REQUEST_INVALID)
    rest = re.sub(r"\s+", "", match.group(2))
    specifiers = tuple(rest.split(",")) if rest else ()
    if any(not _SPECIFIER.fullmatch(s) for s in specifiers):
        raise DependencyPolicyError(DependencyFailureCode.REQUEST_INVALID)
    return DependencyRequest(normalise(match.group(1)), specifiers)


def validate_requests(raw: Sequence[str], *, max_packages: int) -> tuple[DependencyRequest, ...]:
    """Parsed, normalised and sorted requests; duplicates (after normalisation) are conflicts."""
    parsed = [parse_request(r) for r in raw]
    if len(parsed) > max_packages:
        raise DependencyPolicyError(DependencyFailureCode.LIMIT_EXCEEDED)
    names = [p.name for p in parsed]
    if len(set(names)) != len(names):
        raise DependencyPolicyError(DependencyFailureCode.CONFLICT)
    return tuple(sorted(parsed, key=lambda p: p.name))


def wheel_identity(filename: str) -> tuple[str, str]:
    """(normalised name, version) of a wheel file name; anything else is refused."""
    match = _WHEEL.fullmatch(filename)
    if match is None:
        raise DependencyPolicyError(DependencyFailureCode.WHEEL_UNAVAILABLE)
    return normalise(match.group("name")), match.group("version")


def _normalise_hashes(hashes: Sequence[str], *, error_code: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw_hash in hashes:
        token = raw_hash.strip().lower()
        if token.startswith("sha256:"):
            token = token.removeprefix("sha256:")
        if not _SHA256_RE.fullmatch(token):
            raise DependencyPolicyError(error_code, detail="invalid_sha256_hash")
        cleaned.append(token)
    return tuple(sorted(set(cleaned)))


def parse_manifest_requirement(
    raw: str,
    *,
    target_env: TargetEnvironment | None = None,
    hashes: Sequence[str] = (),
    active_extras: frozenset[str] = frozenset(),
    error_code: str = DependencyFailureCode.MANIFEST_INVALID,
    max_length: int = MAX_MANIFEST_REQUEST_LENGTH,
) -> DependencyRequest | None:
    """Parse an untrusted PEP 508 requirement from a repository manifest.

    Returns None when an environment marker does not match ``target_env`` (or ``active_extras``).
    Refuses URLs, paths, VCS refs, credentials, archive filenames and pip flags.
    """
    text = raw.strip()
    if not text or len(text) > max_length or _CONTROL_RE.search(text):
        raise DependencyPolicyError(error_code, detail="empty_or_oversized_requirement")
    if text.startswith("-") or _CREDENTIAL_RE.search(text) or _UNSAFE_URL_OR_PATH_RE.search(text):
        raise DependencyPolicyError(error_code, detail="unsafe_requirement_syntax")
    try:
        req = Requirement(text)
    except InvalidRequirement:
        raise DependencyPolicyError(error_code, detail="invalid_pep508_requirement") from None
    if req.url is not None or req.name.lower().endswith(_ARCHIVE_SUFFIXES):
        raise DependencyPolicyError(error_code, detail="url_or_archive_requirement_refused")
    if req.marker is not None:
        env = target_env or default_target_environment()
        try:
            if active_extras:
                if not any(
                    req.marker.evaluate(env.as_marker_env(extra=ex)) for ex in sorted(active_extras)
                ):
                    return None
            elif not req.marker.evaluate(env.as_marker_env()):
                return None
        except (InvalidMarker, ValueError):
            raise DependencyPolicyError(error_code, detail="invalid_marker") from None
    name = str(canonicalize_name(req.name))
    extras = tuple(sorted({str(canonicalize_name(e)) for e in req.extras}))
    specifiers = tuple(sorted(re.sub(r"\s+", "", str(sp)) for sp in req.specifier))
    norm_hashes = _normalise_hashes(hashes, error_code=error_code)
    return DependencyRequest(name=name, specifiers=specifiers, extras=extras, hashes=norm_hashes)


def validate_python_requirement(
    requires_python: str | None, *, target_env: TargetEnvironment | None = None
) -> None:
    """Ensure ``requires-python`` is valid PEP 440 and allows the execution Python version."""
    if requires_python is None or not requires_python.strip():
        return
    try:
        spec = SpecifierSet(requires_python.strip())
    except InvalidSpecifier:
        raise DependencyPolicyError(
            DependencyFailureCode.MANIFEST_INVALID, detail="invalid_requires_python"
        ) from None
    env = target_env or default_target_environment()
    if not spec.contains(env.python_full_version, prereleases=True):
        raise DependencyPolicyError(
            DependencyFailureCode.PYTHON_UNSUPPORTED, detail=requires_python.strip()
        )


def _bump_release_prefix(release: tuple[int, ...], width: int) -> Version:
    prefix = list(release[:width])
    while len(prefix) < width:
        prefix.append(0)
    prefix[-1] += 1
    return Version(".".join(str(part) for part in prefix))


def _wildcard_bounds(ver_str: str) -> tuple[Version, Version] | None:
    if not ver_str.endswith(".*"):
        return None
    base_str = ver_str[:-2]
    try:
        base = Version(base_str)
    except InvalidVersion:
        return None
    width = len(base.release)
    return base, _bump_release_prefix(base.release, width)


def _compatible_upper(version: Version) -> Version:
    width = max(len(version.release) - 1, 1)
    return _bump_release_prefix(version.release, width)


def _tighten_lower(
    current: tuple[Version, bool] | None, candidate: Version, inclusive: bool
) -> tuple[Version, bool]:
    if current is None:
        return candidate, inclusive
    cur_ver, cur_inc = current
    if candidate > cur_ver:
        return candidate, inclusive
    if candidate == cur_ver:
        return cur_ver, cur_inc and inclusive
    return current


def _tighten_upper(
    current: tuple[Version, bool] | None, candidate: Version, inclusive: bool
) -> tuple[Version, bool]:
    if current is None:
        return candidate, inclusive
    cur_ver, cur_inc = current
    if candidate < cur_ver:
        return candidate, inclusive
    if candidate == cur_ver:
        return cur_ver, cur_inc and inclusive
    return current


def specifiers_compatible(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Return True when two specifier tuples have a non-empty PEP 440 intersection."""
    if not left or not right:
        return True
    try:
        combined = SpecifierSet(",".join((*left, *right)), prereleases=True)
    except InvalidSpecifier:
        return False

    arbitrary_eq = {sp.version for sp in combined if sp.operator == "==="}
    if len(arbitrary_eq) > 1:
        return False
    if arbitrary_eq:
        target = next(iter(arbitrary_eq))
        for sp in combined:
            if sp.operator != "===" and not sp.contains(target, prereleases=True):
                return False
        return True

    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    exact_versions: list[Version] = []
    wildcard_eq: list[Specifier] = []
    wildcard_excl_bounds: list[tuple[Version, Version]] = []

    for sp in combined:
        op = sp.operator
        if op == "==":
            if sp.version.endswith(".*"):
                bounds = _wildcard_bounds(sp.version)
                if bounds is None:
                    return False
                lower = _tighten_lower(lower, bounds[0], True)
                upper = _tighten_upper(upper, bounds[1], False)
                wildcard_eq.append(sp)
            else:
                exact_versions.append(Version(sp.version))
        elif op == "!=" and sp.version.endswith(".*"):
            bounds = _wildcard_bounds(sp.version)
            if bounds is None:
                return False
            wildcard_excl_bounds.append(bounds)
        elif op == "~=":
            ver = Version(sp.version)
            lower = _tighten_lower(lower, ver, True)
            upper = _tighten_upper(upper, _compatible_upper(ver), False)
        elif op == ">=":
            lower = _tighten_lower(lower, Version(sp.version), True)
        elif op == ">":
            lower = _tighten_lower(lower, Version(sp.version), False)
        elif op == "<=":
            upper = _tighten_upper(upper, Version(sp.version), True)
        elif op == "<":
            upper = _tighten_upper(upper, Version(sp.version), False)

    for exact in exact_versions:
        if not combined.contains(exact, prereleases=True):
            return False

    for eq_spec in wildcard_eq:
        for neq_spec in combined:
            if neq_spec.operator == "!=" and neq_spec.version == eq_spec.version:
                return False

    if lower is not None and upper is not None:
        low_ver, low_inc = lower
        high_ver, high_inc = upper
        if low_ver > high_ver:
            return False
        if low_ver == high_ver:
            if not (low_inc and high_inc):
                return False
            if not combined.contains(low_ver, prereleases=True):
                return False
        if wildcard_excl_bounds:
            cur_ver = low_ver
            for ex_low, ex_high in sorted(wildcard_excl_bounds):
                if ex_low > cur_ver:
                    break
                if ex_high > cur_ver:
                    cur_ver = ex_high
            if cur_ver > high_ver:
                return False
            if cur_ver == high_ver and (
                not high_inc or not combined.contains(high_ver, prereleases=True)
            ):
                return False
    return True


def merge_two_requests(a: DependencyRequest, b: DependencyRequest) -> DependencyRequest:
    """Merge two requests for the same normalised package, detecting version or hash conflicts."""
    if a.name != b.name:
        raise DependencyPolicyError(DependencyFailureCode.CONFLICT, detail="package_name_mismatch")
    if not specifiers_compatible(a.specifiers, b.specifiers):
        raise DependencyPolicyError(DependencyFailureCode.CONFLICT, detail=a.name)
    if a.hashes and b.hashes:
        overlap = tuple(sorted(set(a.hashes) & set(b.hashes)))
        if not overlap:
            raise DependencyPolicyError(DependencyFailureCode.CONFLICT, detail=f"{a.name}:hashes")
        hashes = overlap
    else:
        hashes = tuple(sorted(set(a.hashes) | set(b.hashes)))
    specifiers = tuple(sorted(set(a.specifiers) | set(b.specifiers)))
    extras = tuple(sorted(set(a.extras) | set(b.extras)))
    return DependencyRequest(name=a.name, specifiers=specifiers, extras=extras, hashes=hashes)
