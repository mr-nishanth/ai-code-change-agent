"""Trusted documentation hosts for the research tools (`search_docs`, `fetch_reference`).

One version-controlled list of exact host names (`config/reference_sources.yaml`):

    version: 1
    allowed_hosts:
      - pydantic.dev
      - pypi.org
    secondary_hosts:  # optional; ranking only, each must also be in allowed_hosts
      - pypi.org

A host is trusted only when it is listed; every path on it is then allowed (HTML pages, `.md`,
`.txt`, `llms.txt`). Subdomains are never implied, there are no wildcards and no path rules. A
redirect is followed only when its target host is listed too. Both tools use `host_allowed`
and the static URL check `url_refusal`. `secondary_hosts` only orders search results (package
registries and raw source hosts after documentation); it never grants access.
"""

import ipaddress
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT_SOURCES_FILE = Path("config/reference_sources.yaml")
DEFAULT_MAX_URL_CHARS = 300  # characters, for a URL the agent asks for (REFERENCE_MAX_URL_CHARS)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalize_host(raw: str) -> str:
    """The canonical form of a host name: lower case, no trailing dot, IDNA (punycode) ASCII.
    Raises ValueError for anything that is not a plain DNS name (IP literals, wildcards, ports,
    schemes, paths, empty labels)."""
    host = raw.strip().lower()
    if host.endswith("."):
        host = host[:-1]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("invalid_host") from None
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ValueError("invalid_host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("invalid_host")


def host_allowed(host: str, allowed: Collection[str]) -> bool:
    """True only when the normalised host is exactly one of the (normalised) allowed hosts."""
    try:
        return normalize_host(host) in allowed
    except ValueError:
        return False


def url_refusal(
    url: str,
    hosts: Collection[str],
    *,
    contains_secret: Callable[[str], bool],
    requested: bool = True,
    max_url_chars: int = DEFAULT_MAX_URL_CHARS,
) -> str | None:
    """The static (no DNS) part of the URL check shared by fetch_reference and search_docs: the
    refusal code, or None when the URL passes. `requested`: the URL comes from the agent (not a
    server redirect), so it also may not carry a query string, exceed `max_url_chars`, or
    contain a secret (`contains_secret`, the central secret scanner, is injected so this module
    stays free of application imports; it is required, so the check cannot be left out).
    `hosts` are normalised trusted hosts. Raises ValueError for a URL urlsplit cannot parse
    (for example a non-numeric port)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if requested:
        if parts.query:
            return "reference_query_not_allowed"
        if len(url) > max_url_chars:
            return "reference_url_too_long"
        if contains_secret(url):
            return "reference_url_contains_secret"
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return "reference_url_not_allowed"
    if parts.port not in (None, 443):
        return "reference_url_not_allowed"
    if not host_allowed(host, hosts):
        return "reference_domain_not_allowed"
    return None


@dataclass(frozen=True)
class ReferenceSources:
    allowed_hosts: frozenset[str] = frozenset()
    secondary_hosts: frozenset[str] = frozenset()  # ranking only; a subset of allowed_hosts


def _hosts(path: Path, entries: list[object]) -> frozenset[str]:
    hosts: set[str] = set()
    for entry in entries:
        try:
            if not isinstance(entry, str):
                raise ValueError("invalid_host")
            hosts.add(normalize_host(entry))
        except ValueError:
            msg = f"{path}: {entry!r} is not an exact host name (no scheme, port, path or '*')"
            raise ValueError(msg) from None
    return frozenset(hosts)


def load_reference_sources(path: Path) -> ReferenceSources:
    """Read and validate the host file. A missing file means no trusted host (the research tools
    are not offered); a present but invalid file raises ValueError so startup fails. The only
    optional key is `secondary_hosts` (a list of exact hosts, each also in allowed_hosts);
    unknown keys are refused."""
    if not path.is_file():
        return ReferenceSources()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        msg = f"{path}: not readable YAML"
        raise ValueError(msg) from None
    keys = set(data) if isinstance(data, dict) else set()
    if (
        not isinstance(data, dict)
        or not {"version", "allowed_hosts"}
        <= keys
        <= {"version", "allowed_hosts", "secondary_hosts"}
        or data["version"] != 1
        or not isinstance(data["allowed_hosts"], list)
        or not isinstance(data.get("secondary_hosts", []), list)
    ):
        msg = (
            f"{path}: expected 'version: 1', an 'allowed_hosts' list and optionally a "
            "'secondary_hosts' list, nothing else"
        )
        raise ValueError(msg)
    allowed = _hosts(path, data["allowed_hosts"])
    secondary = _hosts(path, data.get("secondary_hosts", []))
    if not secondary <= allowed:
        extra = ", ".join(sorted(secondary - allowed))
        msg = f"{path}: secondary_hosts must also be in allowed_hosts: {extra}"
        raise ValueError(msg)
    return ReferenceSources(allowed, secondary)
