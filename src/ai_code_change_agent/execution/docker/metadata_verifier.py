"""Cross-check downloaded wheels against the package index's published hashes (ADR-010).

The resolver computes sha256 from the bytes it received. That alone proves only "what we got",
not "what the index publishes". Before a bundle is published, the worker independently fetches the
index's own listing for each package over HTTPS (TLS verified, redirects refused, size bounded) and
requires the exact wheel filename to be listed with the same sha256:

- PEP 691 JSON (`Accept: application/vnd.pypi.simple.v1+json`): `files[].hashes.sha256`;
- PEP 503 HTML fallback: the `#sha256=<hex>` fragment of the file link.

Every standard simple index (PyPI, devpi, Artifactory, Nexus, CodeArtifact) serves one of these.
If the index publishes no sha256 for the file, the wheel is NOT treated as verified: the bundle
fails with `dependency_metadata_unavailable` unless the operator explicitly set
`DEPENDENCY_ALLOW_UNVERIFIED_INDEX=true`, in which case the result is recorded as `unverified`.
A published hash that differs fails with `dependency_hash_mismatch`.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from packaging.utils import canonicalize_name

from ai_code_change_agent.application.dependencies.ports import LockedPackage
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.dependencies.policy import (
    DependencyFailureCode,
    DependencyPolicyError,
)

_log = get_logger("metadata_verifier")

Source = Literal["pep691_json", "pep503_html", "unverified"]
_JSON = "application/vnd.pypi.simple.v1+json"
_MAX_BYTES = 8 * 1024 * 1024  # simple pages of large projects can be a few MB
_HREF = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{64}$")

Fetch = Callable[[str], tuple[int, str, bytes]]  # url -> (status, content-type, body)


@dataclass(frozen=True)
class WheelVerification:
    filename: str
    sha256: str
    published_sha256: str | None
    source: Source
    # PEP 740: the index lists a provenance (attestation) URL for this file. Recorded for audit
    # only; the attestation itself is NOT cryptographically verified here (see ADR-010).
    attestation_published: bool = False

    def as_dict(self) -> dict[str, str | bool | None]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "published_sha256": self.published_sha256,
            "source": self.source,
            "attestation_published": self.attestation_published,
        }


def _https_fetch(url: str) -> tuple[int, str, bytes]:
    headers = {"Accept": f"{_JSON}, text/html;q=0.1"}
    with httpx.Client(follow_redirects=False, timeout=20.0, verify=True) as client:  # noqa: SIM117
        with client.stream("GET", url, headers=headers) as resp:
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > _MAX_BYTES:
                    raise DependencyPolicyError(DependencyFailureCode.METADATA_UNAVAILABLE)
            return resp.status_code, resp.headers.get("content-type", ""), bytes(body)


def attested_files(content_type: str, body: bytes) -> frozenset[str]:
    """Filenames for which a PEP 691 JSON page lists a PEP 740 provenance URL."""
    if content_type.split(";")[0].strip() != _JSON:
        return frozenset()
    import json

    data = json.loads(body)
    files = data.get("files", []) if isinstance(data, dict) else []
    return frozenset(
        f["filename"]
        for f in files
        if isinstance(f, dict) and isinstance(f.get("filename"), str) and f.get("provenance")
    )


def published_hashes(page_url: str, content_type: str, body: bytes) -> dict[str, str]:
    """filename -> sha256 published by one simple-index project page (JSON or HTML)."""
    out: dict[str, str] = {}
    if content_type.split(";")[0].strip() == _JSON:
        import json

        data = json.loads(body)
        for f in data.get("files", []) if isinstance(data, dict) else []:
            if not isinstance(f, dict):
                continue
            name, hashes = f.get("filename"), f.get("hashes")
            sha = hashes.get("sha256") if isinstance(hashes, dict) else None
            if isinstance(name, str) and isinstance(sha, str) and _SHA.match(sha.lower()):
                out[name] = sha.lower()
        return out
    for href in _HREF.findall(body.decode("utf-8", errors="replace")):
        url = urljoin(page_url, html.unescape(href))
        parts = urlsplit(url)
        algo, _, value = parts.fragment.partition("=")
        name = unquote(parts.path.rsplit("/", 1)[-1])
        if algo == "sha256" and _SHA.match(value.lower()) and name:
            out[name] = value.lower()
    return out


class MetadataVerifier:
    def __init__(
        self,
        index_url: str,
        *,
        allow_unverified_index: bool = False,
        fetch: Fetch | None = None,
    ) -> None:
        if urlsplit(index_url).scheme != "https":
            raise ValueError("the package index must use https")
        self._index = index_url.rstrip("/") + "/"
        self._allow_unverified = allow_unverified_index
        self._fetch = fetch or _https_fetch

    def _page(self, name: str) -> tuple[str, dict[str, str], frozenset[str]]:
        url = urljoin(self._index, f"{canonicalize_name(name)}/")
        try:
            status, ctype, body = self._fetch(url)
        except DependencyPolicyError:
            raise
        except (httpx.HTTPError, OSError, ValueError):
            _log.warning("index_metadata_fetch_failed", package=name)
            raise DependencyPolicyError(DependencyFailureCode.METADATA_UNAVAILABLE) from None
        if status != 200:  # redirects are refused, not followed
            _log.warning("index_metadata_status", package=name, status=status)
            raise DependencyPolicyError(DependencyFailureCode.METADATA_UNAVAILABLE)
        try:
            hashes = published_hashes(url, ctype, body)
            attested = attested_files(ctype, body)
        except ValueError:
            raise DependencyPolicyError(DependencyFailureCode.METADATA_UNAVAILABLE) from None
        source = "pep691_json" if ctype.split(";")[0].strip() == _JSON else "pep503_html"
        return source, hashes, attested

    def verify(self, packages: Iterable[LockedPackage]) -> tuple[WheelVerification, ...]:
        results: list[WheelVerification] = []
        pages: dict[str, tuple[str, dict[str, str], frozenset[str]]] = {}
        for p in packages:
            if p.name not in pages:
                pages[p.name] = self._page(p.name)
            source, hashes, attested = pages[p.name]
            published = hashes.get(p.filename)
            if published is None:
                if not self._allow_unverified:
                    _log.error("wheel_hash_not_published", filename=p.filename)
                    raise DependencyPolicyError(DependencyFailureCode.METADATA_UNAVAILABLE)
                _log.warning("wheel_hash_unverified_allowed", filename=p.filename)
                results.append(WheelVerification(p.filename, p.sha256, None, "unverified"))
                continue
            if published != p.sha256:
                _log.error("wheel_hash_mismatch", filename=p.filename, source=source)
                raise DependencyPolicyError(DependencyFailureCode.HASH_MISMATCH)
            results.append(
                WheelVerification(
                    p.filename, p.sha256, published, source,  # type: ignore[arg-type]
                    attestation_published=p.filename in attested,
                )
            )  # fmt: skip
        return tuple(results)
