"""Harness-side reference fetching (ADR-011): permission rules are enforced by the fetcher, not
the model, and the agent tool returns untrusted, budgeted text."""

from collections.abc import Callable

import httpx
import pytest

from ai_code_change_agent.agents.coding.tools import build_reference_tool
from ai_code_change_agent.application.references.ports import ReferenceRefusedError
from ai_code_change_agent.integrations.web.fetcher import HttpReferenceFetcher, html_to_text

PUBLIC = ["104.16.0.1"]


def fetcher(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    resolve: dict[str, list[str]] | None = None,
    max_bytes: int = 1_048_576,
) -> HttpReferenceFetcher:
    table = resolve or {}

    async def resolver(host: str) -> list[str]:
        return table.get(host, PUBLIC)

    return HttpReferenceFetcher(
        allowed_domains=["docs.langchain.com", "python.org"],
        transport=httpx.MockTransport(handler),
        resolver=resolver,
        max_bytes=max_bytes,
    )


def ok(text: str, ctype: str = "text/plain") -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": ctype})


async def test_an_allow_listed_text_document_is_fetched() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok("# LangChain\n- [Agents](https://docs.langchain.com/agents.md)")

    doc = await fetcher(handler).fetch("https://docs.langchain.com/llms.txt")
    assert doc.text.startswith("# LangChain") and not doc.truncated
    (req,) = seen
    assert (
        req.method == "GET" and "authorization" not in req.headers and "cookie" not in req.headers
    )


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://docs.langchain.com/llms.txt", "reference_url_not_allowed"),
        ("https://user:pw@docs.langchain.com/x", "reference_url_not_allowed"),
        ("https://docs.langchain.com:8443/x", "reference_url_not_allowed"),
        ("https://evil.invalid/llms.txt", "reference_domain_not_allowed"),
        ("https://docs.langchain.com.evil.invalid/x", "reference_domain_not_allowed"),
        ("https://notpython.org/x", "reference_domain_not_allowed"),
        ("file:///etc/passwd", "reference_url_not_allowed"),
    ],
)
async def test_urls_outside_the_rules_are_refused_without_a_request(url: str, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError

    with pytest.raises(ReferenceRefusedError, match=code):
        await fetcher(handler).fetch(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.2"]
)
async def test_allow_listed_hosts_resolving_to_internal_addresses_are_refused(address: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError

    f = fetcher(handler, resolve={"docs.python.org": [address]})
    with pytest.raises(ReferenceRefusedError, match="reference_address_not_public"):
        await f.fetch("https://docs.python.org/3/")


async def test_redirects_are_rechecked_and_capped() -> None:
    def off_domain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.invalid/steal"})

    with pytest.raises(ReferenceRefusedError, match="reference_domain_not_allowed"):
        await fetcher(off_domain).fetch("https://docs.python.org/a")

    def within(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return ok("moved")

    assert (await fetcher(within).fetch("https://docs.python.org/a")).url.endswith("/b")

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(ReferenceRefusedError, match="reference_too_many_redirects"):
        await fetcher(loop).fetch("https://docs.python.org/a")


async def test_binary_content_errors_and_size_limits() -> None:
    def binary(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00", headers={"content-type": "application/zip"})

    with pytest.raises(ReferenceRefusedError, match="reference_content_type_not_text"):
        await fetcher(binary).fetch("https://docs.python.org/x.zip")

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(ReferenceRefusedError, match="reference_http_404"):
        await fetcher(missing).fetch("https://docs.python.org/nope")

    def big(request: httpx.Request) -> httpx.Response:
        return ok("x" * 5000)

    doc = await fetcher(big, max_bytes=2048).fetch("https://docs.python.org/big")
    assert doc.truncated and len(doc.text) == 2048

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(ReferenceRefusedError, match="reference_unavailable"):
        await fetcher(broken).fetch("https://docs.python.org/x")


async def test_unresolvable_hosts_are_refused() -> None:
    async def fail(host: str) -> list[str]:
        raise OSError

    f = HttpReferenceFetcher(allowed_domains=["python.org"], resolver=fail)
    with pytest.raises(ReferenceRefusedError, match="reference_unresolvable"):
        await f.fetch("https://docs.python.org/")
    assert "python.org" in repr(f)


def test_html_is_reduced_to_text_without_scripts() -> None:
    html = (
        "<html><head><title>t</title><script>steal()</script></head><body><h1>Agents</h1>"
        "<p>Use <a href='https://docs.langchain.com/a'>this</a>.</p><style>x{}</style></body></html>"
    )
    text = html_to_text(html)
    assert "steal" not in text and "x{}" not in text
    assert "Agents" in text and "[https://docs.langchain.com/a]" in text


async def test_the_agent_tool_marks_untrusted_data_and_enforces_its_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok("<h1>Doc</h1>", "text/html")

    tool = build_reference_tool(fetcher(handler), max_calls=2)
    assert "docs.langchain.com" in tool.description
    first = await tool.ainvoke({"url": "https://docs.langchain.com/x"})
    assert "UNTRUSTED DATA BEGINS" in first and "Doc" in first
    refused = await tool.ainvoke({"url": "https://evil.invalid/x"})
    assert refused == "REFUSED: reference_domain_not_allowed"
    assert "reference_budget_exhausted" in await tool.ainvoke({"url": "https://python.org/"})


async def test_html_with_megabytes_of_head_scripts_still_yields_its_body_text() -> None:
    head = "<html><head><script>" + "x" * 3_000_000 + "</script></head>"
    page = (head + "<body><h1>Jira REST v3</h1><p>Use basic auth.</p></body></html>").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=page)

    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    f = HttpReferenceFetcher(
        allowed_domains=["docs.example.com"], max_bytes=1_048_576,
        transport=httpx.MockTransport(handler), resolver=public,
    )  # fmt: skip
    doc = await f.fetch("https://docs.example.com/page")
    assert "Jira REST v3" in doc.text and "Use basic auth." in doc.text
    assert "xxxx" not in doc.text and doc.retrieved_at.endswith("+00:00")
