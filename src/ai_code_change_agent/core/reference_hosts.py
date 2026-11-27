"""Trusted documentation hosts for the research tools (`search_docs`, `fetch_reference`).

One version-controlled list of exact host names (`config/reference_sources.yaml`):

    version: 1
    allowed_hosts:
      - pydantic.dev

A host is trusted only when it is listed; every path on it is then allowed (HTML pages, `.md`,
`.txt`, `llms.txt`). Subdomains are never implied, there are no wildcards and no path rules. A
redirect is followed only when its target host is listed too. Both tools use `host_allowed`.
"""

import ipaddress
import re
from collections.abc import Collection
from pathlib import Path

import yaml

DEFAULT_SOURCES_FILE = Path("config/reference_sources.yaml")
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


def load_allowed_hosts(path: Path) -> frozenset[str]:
    """Read and validate the host list. A missing file means no trusted host (the research tools
    are not offered); a present but invalid file raises ValueError so startup fails."""
    if not path.is_file():
        return frozenset()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        msg = f"{path}: not readable YAML"
        raise ValueError(msg) from None
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "allowed_hosts"}
        or data["version"] != 1
        or not isinstance(data["allowed_hosts"], list)
    ):
        msg = f"{path}: expected 'version: 1' and an 'allowed_hosts' list, nothing else"
        raise ValueError(msg)
    hosts: set[str] = set()
    for entry in data["allowed_hosts"]:
        try:
            if not isinstance(entry, str):
                raise ValueError("invalid_host")
            hosts.add(normalize_host(entry))
        except ValueError:
            msg = f"{path}: {entry!r} is not an exact host name (no scheme, port, path or '*')"
            raise ValueError(msg) from None
    return frozenset(hosts)
