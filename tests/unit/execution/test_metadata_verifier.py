"""Published-hash cross-check against the package index (PEP 691 JSON / PEP 503 HTML)."""

import json

import pytest

from ai_code_change_agent.application.dependencies.ports import LockedPackage
from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError
from ai_code_change_agent.execution.docker.metadata_verifier import (
    MetadataVerifier,
    published_hashes,
)

INDEX = "https://pypi.org/simple"
SHA = "a" * 64
WHEEL = "six-1.16.0-py2.py3-none-any.whl"
PKG = LockedPackage("six", "1.16.0", WHEEL, SHA, 10)
JSON_CT = "application/vnd.pypi.simple.v1+json"


def json_page(sha: str = SHA, name: str = WHEEL) -> bytes:
    return json.dumps({"files": [{"filename": name, "hashes": {"sha256": sha}}]}).encode()


def fetcher(status: int, ctype: str, body: bytes, seen: list[str] | None = None):  # type: ignore[no-untyped-def]
    def fetch(url: str) -> tuple[int, str, bytes]:
        if seen is not None:
            seen.append(url)
        return status, ctype, body

    return fetch


def test_matching_pep691_hash_is_verified_and_recorded() -> None:
    seen: list[str] = []
    v = MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, json_page(), seen))
    (res,) = v.verify([PKG])
    assert (res.source, res.published_sha256) == ("pep691_json", SHA)
    assert seen == ["https://pypi.org/simple/six/"]


def test_pep503_html_fragment_is_used_when_json_is_not_served() -> None:
    body = f'<a href="https://files.example/p/{WHEEL}#sha256={SHA}">{WHEEL}</a>'.encode()
    v = MetadataVerifier(INDEX, fetch=fetcher(200, "text/html", body))
    assert v.verify([PKG])[0].source == "pep503_html"


def test_a_different_published_hash_is_refused() -> None:
    v = MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, json_page("b" * 64)))
    with pytest.raises(DependencyPolicyError, match="dependency_hash_mismatch"):
        v.verify([PKG])


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, json_page(name="other.whl")), (404, b""), (301, b""), (200, json.dumps({}).encode())],
)
def test_unpublished_or_unreachable_metadata_fails_closed(status: int, body: bytes) -> None:
    v = MetadataVerifier(INDEX, fetch=fetcher(status, JSON_CT, body))
    with pytest.raises(DependencyPolicyError, match="dependency_metadata_unavailable"):
        v.verify([PKG])


def test_fetch_errors_and_bad_json_fail_closed() -> None:
    def boom(url: str) -> tuple[int, str, bytes]:
        raise OSError("down")

    with pytest.raises(DependencyPolicyError, match="dependency_metadata_unavailable"):
        MetadataVerifier(INDEX, fetch=boom).verify([PKG])
    with pytest.raises(DependencyPolicyError, match="dependency_metadata_unavailable"):
        MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, b"{nope")).verify([PKG])


def test_unverified_is_only_accepted_when_the_operator_allows_it_and_is_labelled() -> None:
    v = MetadataVerifier(
        INDEX, allow_unverified_index=True, fetch=fetcher(200, JSON_CT, json_page(name="x.whl"))
    )
    (res,) = v.verify([PKG])
    assert (res.source, res.published_sha256) == ("unverified", None)
    # a mismatch is never excused by the operator switch
    v2 = MetadataVerifier(
        INDEX, allow_unverified_index=True, fetch=fetcher(200, JSON_CT, json_page("b" * 64))
    )
    with pytest.raises(DependencyPolicyError, match="dependency_hash_mismatch"):
        v2.verify([PKG])


def test_one_request_per_project_and_plain_http_index_is_refused() -> None:
    seen: list[str] = []
    pkgs = [PKG, LockedPackage("six", "1.16.0", WHEEL, SHA, 10)]
    MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, json_page(), seen)).verify(pkgs)
    assert len(seen) == 1
    with pytest.raises(ValueError, match="https"):
        MetadataVerifier("http://pypi.org/simple")


def test_published_hashes_ignores_malformed_entries() -> None:
    body = json.dumps(
        {"files": ["x", {"filename": "a.whl", "hashes": {"sha256": "short"}}, {"filename": 1}]}
    ).encode()
    assert published_hashes("https://i/simple/a/", JSON_CT, body) == {}


def test_pep740_attestation_availability_is_recorded_not_claimed_verified() -> None:
    body = json.dumps(
        {
            "files": [
                {
                    "filename": WHEEL,
                    "hashes": {"sha256": SHA},
                    "provenance": "https://pypi.org/integrity/six/1.16.0/x/provenance",
                }
            ]
        }
    ).encode()
    (res,) = MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, body)).verify([PKG])
    assert res.attestation_published and res.as_dict()["attestation_published"] is True
    (plain,) = MetadataVerifier(INDEX, fetch=fetcher(200, JSON_CT, json_page())).verify([PKG])
    assert not plain.attestation_published


def _transport(size: int, status: int = 200):  # type: ignore[no-untyped-def]
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        return httpx.Response(status, headers={"content-type": JSON_CT}, content=b"x" * size)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("size", [0, 1023, 1024])
def test_index_pages_up_to_the_cap_are_read_whole(size: int) -> None:
    from ai_code_change_agent.execution.docker.metadata_verifier import _https_fetch

    status, ctype, body = _https_fetch(
        "https://pypi.org/simple/x/", max_bytes=1024, transport=_transport(size)
    )
    assert (status, ctype, len(body)) == (200, JSON_CT, size)


def test_an_index_page_over_the_cap_fails_closed() -> None:
    from ai_code_change_agent.execution.docker.metadata_verifier import _https_fetch

    with pytest.raises(DependencyPolicyError, match="dependency_metadata_unavailable"):
        _https_fetch("https://pypi.org/simple/x/", max_bytes=1024, transport=_transport(1025))


def test_the_production_cap_fits_the_largest_known_pypi_page() -> None:
    from ai_code_change_agent.execution.docker import metadata_verifier

    assert metadata_verifier._MAX_BYTES == 64 * 1024 * 1024  # pydantic-core: ~10 MB (2026-11)


def test_a_redirect_from_the_index_is_never_followed() -> None:
    import httpx

    from ai_code_change_agent.execution.docker.metadata_verifier import _https_fetch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/simple/x/"})

    status, _, _ = _https_fetch(
        "https://pypi.org/simple/x/", transport=httpx.MockTransport(handler)
    )
    assert status == 302  # the verifier then refuses: dependency_metadata_unavailable
