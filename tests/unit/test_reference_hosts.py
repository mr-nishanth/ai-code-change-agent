"""Exact-host trust for the research tools: one shared check, a version-controlled host list,
every path on a listed host, no implied subdomains, every redirect hop checked."""

import re
from pathlib import Path

import httpx
import pytest

from ai_code_change_agent.application.references.ports import ReferenceRefusedError
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.reference_hosts import (
    host_allowed,
    load_allowed_hosts,
    normalize_host,
)
from ai_code_change_agent.integrations.web.fetcher import HttpReferenceFetcher

TRUSTED = frozenset({"pydantic.dev", "docs.python.org"})


@pytest.mark.parametrize(
    "host",
    ["pydantic.dev", "PYDANTIC.DEV", "Pydantic.Dev", "pydantic.dev.", " pydantic.dev "],
)
def test_listed_host_matches_case_insensitively_and_without_trailing_dot(host: str) -> None:
    assert host_allowed(host, TRUSTED)


@pytest.mark.parametrize(
    "host",
    [
        "docs.pydantic.dev",  # subdomain: never implied
        "anything.pydantic.dev",
        "pydantic.dev.attacker.example",  # look-alikes
        "pydantic.example",
        "evilpydantic.dev",
        "python.org",  # parent of a listed host
        "",
        "*.pydantic.dev",
        "127.0.0.1",
        "pydantic..dev",
    ],
)
def test_unlisted_subdomain_parent_and_look_alike_hosts_are_refused(host: str) -> None:
    assert not host_allowed(host, TRUSTED)


def test_idna_hosts_are_compared_in_punycode() -> None:
    assert normalize_host("bücher.example") == "xn--bcher-kva.example"
    assert host_allowed("BÜCHER.example", frozenset({"xn--bcher-kva.example"}))
    assert not host_allowed("bucher.example", frozenset({"xn--bcher-kva.example"}))


@pytest.mark.parametrize(
    "bad",
    ["*.pydantic.dev", "https://pydantic.dev", "pydantic.dev/docs", "pydantic.dev:443",
     "localhost", "10.0.0.1", "[::1]", "a b.dev", "user@pydantic.dev", "-x.dev"],
)  # fmt: skip
def test_normalize_rejects_anything_but_a_plain_host_name(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid_host"):
        normalize_host(bad)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "reference_sources.yaml"
    path.write_text(text)
    return path


def test_the_host_file_is_loaded_and_normalised(tmp_path: Path) -> None:
    path = write(tmp_path, "version: 1\nallowed_hosts:\n  - Pydantic.dev.\n  - docs.python.org\n")
    assert load_allowed_hosts(path) == TRUSTED


def test_a_missing_host_file_means_no_trusted_host(tmp_path: Path) -> None:
    assert load_allowed_hosts(tmp_path / "absent.yaml") == frozenset()


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nallowed_hosts:\n  - '*.pydantic.dev'\n",
        "version: 1\nallowed_hosts:\n  - https://pydantic.dev/docs\n",
        "version: 1\nallowed_hosts:\n  - 3\n",
        "version: 2\nallowed_hosts: []\n",
        "version: 1\nallowed_hosts: pydantic.dev\n",
        "version: 1\nallowed_hosts: []\nallowed_paths: ['/docs']\n",
        "allowed_hosts: [pydantic.dev]\n",
        "version: 1\nallowed_hosts: [unterminated\n",
    ],
)
def test_an_invalid_host_file_fails(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError, match=r"reference_sources\.yaml"):
        load_allowed_hosts(write(tmp_path, text))


def test_settings_load_the_host_file_and_fail_on_an_invalid_one(tmp_path: Path) -> None:
    good = write(tmp_path, "version: 1\nallowed_hosts: [pydantic.dev]\n")
    s = Settings(_env_file=None, reference_sources_file=good)
    assert s.reference_allowed_hosts == frozenset({"pydantic.dev"})
    bad = write(tmp_path, "version: 1\nallowed_hosts: ['*.dev']\n")
    with pytest.raises(ValueError, match="not an exact host name"):
        Settings(_env_file=None, reference_sources_file=bad)


def test_the_repository_host_file_is_valid() -> None:
    hosts = load_allowed_hosts(Path(__file__).parents[2] / "config" / "reference_sources.yaml")
    assert hosts and all(normalize_host(h) == h for h in hosts)


# ---- fetching under the exact-host policy -------------------------------------------------------

LLMS_TXT = """# Pydantic Validation
> Data validation using Python type hints.
## Concepts
- [Validators](https://pydantic.dev/docs/validation/latest/concepts/validators/index.md): field and model validators
- [Fields](https://pydantic.dev/docs/validation/latest/concepts/fields/index.md): field customisation
- [Elsewhere](https://evil.example/validators.md): not trusted
"""
VALIDATORS_MD = "# Validators\n\nUse `@field_validator('vin', mode='before')` to normalise input.\n"


def site(routes: dict[str, httpx.Response]) -> HttpReferenceFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        return routes.get(url, httpx.Response(404, text="missing"))

    async def public(host: str) -> list[str]:
        return {"localhost": ["127.0.0.1"], "internal.pydantic.dev": ["10.1.2.3"]}.get(
            host, ["104.16.0.1"]
        )

    return HttpReferenceFetcher(
        allowed_hosts=sorted(TRUSTED | {"docs.pydantic.dev", "internal.pydantic.dev"}),
        transport=httpx.MockTransport(handler),
        resolver=public,
        max_bytes=4096,
    )


def text(body: str, ctype: str = "text/plain; charset=utf-8") -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": ctype})


async def test_llms_txt_index_then_a_linked_markdown_page_can_be_used() -> None:
    page = "https://pydantic.dev/docs/validation/latest/concepts/validators/index.md"
    f = site({
        "https://pydantic.dev/docs/validation/latest/llms.txt": text(LLMS_TXT),
        page: text(VALIDATORS_MD, "text/markdown; charset=utf-8"),
    })  # fmt: skip
    index = await f.fetch("https://pydantic.dev/docs/validation/latest/llms.txt")
    assert index.content_type == "text/plain" and not index.truncated
    links = re.findall(r"\]\((https://[^)\s]+)\)", index.text)  # what the agent reads
    assert page in links
    usable = [u for u in links if host_allowed(httpx.URL(u).host, f.allowed_hosts)]
    assert "https://evil.example/validators.md" not in usable
    doc = await f.fetch(usable[0])
    assert doc.url == page and "field_validator" in doc.text and doc.content_type == "text/markdown"
    with pytest.raises(ReferenceRefusedError, match="reference_domain_not_allowed"):
        await f.fetch("https://evil.example/validators.md")


async def test_llms_full_txt_is_cut_at_the_byte_limit() -> None:
    url = "https://pydantic.dev/docs/validation/latest/llms-full.txt"
    doc = await site({url: text("x" * 50_000)}).fetch(url)
    assert doc.truncated and len(doc.text) == 4096


async def test_non_text_content_is_refused() -> None:
    url = "https://pydantic.dev/docs/archive.zip"
    f = site({url: httpx.Response(200, content=b"PK", headers={"content-type": "application/zip"})})
    with pytest.raises(ReferenceRefusedError, match="reference_content_type_not_text"):
        await f.fetch(url)


def hop(location: str) -> httpx.Response:
    return httpx.Response(301, headers={"location": location})


async def test_a_chain_of_listed_hosts_is_followed() -> None:
    f = site({
        "https://docs.pydantic.dev/latest/": hop("https://pydantic.dev/docs/"),
        "https://pydantic.dev/docs/": hop("https://pydantic.dev/docs/validation/latest/"),
        "https://pydantic.dev/docs/validation/latest/": text("<p>Welcome</p>", "text/html"),
    })  # fmt: skip
    doc = await f.fetch("https://docs.pydantic.dev/latest/")
    assert doc.url == "https://pydantic.dev/docs/validation/latest/" and "Welcome" in doc.text


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("https://other.pydantic.dev/x", "reference_redirect_not_allowed:other.pydantic.dev"),
        ("http://pydantic.dev/docs/", "reference_redirect_not_allowed:pydantic.dev"),
        ("https://localhost/x", "reference_redirect_not_allowed:localhost"),
        ("https://internal.pydantic.dev/x", "reference_redirect_not_allowed:internal.pydantic.dev"),
        ("https://10.0.0.5/x", "reference_redirect_not_allowed:10.0.0.5"),
    ],
)
async def test_unlisted_downgraded_and_private_redirects_are_refused(
    location: str, code: str
) -> None:
    f = site({"https://pydantic.dev/docs/": hop(location)})
    with pytest.raises(ReferenceRefusedError) as err:
        await f.fetch("https://pydantic.dev/docs/")
    assert err.value.code == code


async def test_the_redirect_limit_is_enforced() -> None:
    f = site({f"https://pydantic.dev/{i}": hop(f"https://pydantic.dev/{i + 1}") for i in range(6)})
    with pytest.raises(ReferenceRefusedError, match="reference_too_many_redirects"):
        await f.fetch("https://pydantic.dev/0")


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://pydantic.dev/docs/llms.txt?goal=align+VIN+for+KAN-12&organization=acme",
         "reference_query_not_allowed"),
        ("https://pydantic.dev/docs/?q=x", "reference_query_not_allowed"),
        ("https://pydantic.dev/docs/" + "a" * 400, "reference_url_too_long"),
        ("https://pydantic.dev/docs/ghp_0123456789abcdefghijklmnopqrstuvwxyz",
         "reference_url_contains_secret"),
    ],
)  # fmt: skip
async def test_agent_urls_cannot_carry_story_or_secret_data(url: str, code: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return text("never")

    f = HttpReferenceFetcher(
        allowed_hosts=["pydantic.dev"], transport=httpx.MockTransport(handler),
        resolver=lambda host: _public(),
    )  # fmt: skip
    with pytest.raises(ReferenceRefusedError) as err:
        await f.fetch(url)
    assert err.value.code == code and seen == []


async def _public() -> list[str]:
    return ["104.16.0.1"]


async def test_fragments_are_dropped_and_server_redirects_may_carry_a_query() -> None:
    f = site({
        "https://pydantic.dev/docs/": hop("https://pydantic.dev/docs/?v=latest"),
        "https://pydantic.dev/docs/?v=latest": text("ok"),
    })  # fmt: skip
    doc = await f.fetch("https://pydantic.dev/docs/#validators")
    assert doc.url == "https://pydantic.dev/docs/?v=latest" and doc.text == "ok"


async def test_relative_llms_txt_links_are_fetched_as_absolute_urls_on_the_same_host() -> None:
    index_url = "https://pydantic.dev/docs/validation/latest/llms.txt"
    page = "https://pydantic.dev/docs/validation/latest/concepts/validators/index.md"
    llms = (
        "# Pydantic\n"
        "- [Validators](/docs/validation/latest/concepts/validators/index.md)\n"
        "- [Mirror](//evil.example/validators.md)\n"
    )
    f = site({index_url: text(llms), page: text(VALIDATORS_MD, "text/markdown")})
    index = await f.fetch(index_url)
    links = re.findall(r"\]\(([^)\s]+)\)", index.text)
    assert links == ["/docs/validation/latest/concepts/validators/index.md",
                     "//evil.example/validators.md"]  # fmt: skip
    # a relative path is not a URL the fetcher accepts on its own ...
    with pytest.raises(ReferenceRefusedError, match="reference_url_not_allowed"):
        await f.fetch(links[0])
    # ... resolved against the index it is an absolute URL on the same, listed host
    absolute = [str(httpx.URL(index.url).join(link)) for link in links]
    assert absolute[0] == page
    doc = await f.fetch(absolute[0])
    assert doc.url == page and "field_validator" in doc.text
    # a protocol-relative link resolves to another host and stays refused
    assert absolute[1] == "https://evil.example/validators.md"
    with pytest.raises(ReferenceRefusedError, match="reference_domain_not_allowed"):
        await f.fetch(absolute[1])
