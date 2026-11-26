"""Python dependency requests from an implementation plan, validated by the platform.

The agent may only *request* packages: a PEP 508 name with optional version specifiers. URLs, file
paths, VCS references, extras and environment markers are refused before anything is fetched.
Trusted infrastructure resolves, downloads (wheels only) and hash-locks them
(docs/decisions/ADR-010-dynamic-dependency-provisioning.md).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

MAX_REQUEST_LENGTH = 200
_REQUEST = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)\s*(.*)")
_SPECIFIER = re.compile(r"(?:===|==|~=|!=|<=|>=|<|>)[0-9][A-Za-z0-9.*+!_-]{0,40}")
_WHEEL = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?)-(?P<version>[A-Za-z0-9.!+_]+)"
    r"(?:-[0-9][A-Za-z0-9._]*)?-[A-Za-z0-9._]+-[A-Za-z0-9._]+-[A-Za-z0-9._]+\.whl"
)


class DependencyPolicyError(Exception):
    """A request or a prepared bundle violates the dependency policy. The message is the code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class DependencyRequest:
    name: str  # normalised
    specifiers: tuple[str, ...] = ()

    def requirement(self) -> str:
        return self.name + ",".join(self.specifiers)


def parse_request(raw: str) -> DependencyRequest:
    text = raw.strip()
    match = _REQUEST.fullmatch(text) if 0 < len(text) <= MAX_REQUEST_LENGTH else None
    if match is None:
        raise DependencyPolicyError("dependency_request_invalid")
    rest = re.sub(r"\s+", "", match.group(2))
    specifiers = tuple(rest.split(",")) if rest else ()
    if any(not _SPECIFIER.fullmatch(s) for s in specifiers):
        raise DependencyPolicyError("dependency_request_invalid")
    return DependencyRequest(normalise(match.group(1)), specifiers)


def validate_requests(raw: Sequence[str], *, max_packages: int) -> tuple[DependencyRequest, ...]:
    """Parsed, normalised and sorted requests; duplicates (after normalisation) are conflicts."""
    parsed = [parse_request(r) for r in raw]
    if len(parsed) > max_packages:
        raise DependencyPolicyError("dependency_limit_exceeded")
    names = [p.name for p in parsed]
    if len(set(names)) != len(names):
        raise DependencyPolicyError("dependency_conflict")
    return tuple(sorted(parsed, key=lambda p: p.name))


def wheel_identity(filename: str) -> tuple[str, str]:
    """(normalised name, version) of a wheel file name; anything else is refused."""
    match = _WHEEL.fullmatch(filename)
    if match is None:
        raise DependencyPolicyError("dependency_wheel_unavailable")
    return normalise(match.group("name")), match.group("version")
