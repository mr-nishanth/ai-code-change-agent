"""Harness-side documentation search: domain filtering, secrets, budgets and provenance."""

from types import SimpleNamespace
from typing import Any

from ai_code_change_agent.agents.coding.tools import build_search_tool
from ai_code_change_agent.application.references.ports import (
    SearchHit,
    SearchResult,
)
from ai_code_change_agent.integrations.web.search import AnthropicDocumentSearch


class FakeMessages:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        usage = SimpleNamespace(
            input_tokens=1000,
            output_tokens=20,
            server_tool_use=SimpleNamespace(web_search_requests=1),
        )
        return SimpleNamespace(content=self.content, usage=usage)


def result_block(*urls: str) -> Any:
    items = [SimpleNamespace(url=u, title=f"T {u}", page_age=None) for u in urls]
    return SimpleNamespace(type="web_search_tool_result", content=items)


def provider(content: list[Any]) -> tuple[AnthropicDocumentSearch, FakeMessages]:
    messages = FakeMessages(content)
    client = SimpleNamespace(messages=messages)
    s = AnthropicDocumentSearch(
        api_key="sk-test", model="claude-haiku-5-5",
        allowed_hosts=["docs.python.org", "fastapi.tiangolo.com"], client=client,
    )  # fmt: skip
    return s, messages


async def test_only_https_urls_on_allowed_hosts_are_returned_with_a_timestamp() -> None:
    s, messages = provider(
        [
            result_block(
                "https://fastapi.tiangolo.com/tutorial/",
                "https://evil.example.com/fastapi",
                "http://docs.python.org/3/",
                "https://docs.python.org.evil.com/x",
                "https://docs.python.org/3/library/asyncio.html",
            )
        ]
    )
    r = await s.search("  fastapi   dependencies  ")
    assert [h.url for h in r.hits] == [
        "https://fastapi.tiangolo.com/tutorial/",
        "https://docs.python.org/3/library/asyncio.html",
    ]
    assert r.query == "fastapi dependencies" and r.retrieved_at.endswith("+00:00")
    tool = messages.calls[0]["tools"][0]
    assert tool["allowed_domains"] == ["docs.python.org", "fastapi.tiangolo.com"]
    assert tool["max_uses"] == 1 and "sk-test" not in repr(s)


async def test_a_search_error_object_is_refused() -> None:
    s, _ = provider(
        [
            SimpleNamespace(
                type="web_search_tool_result",
                content=SimpleNamespace(error_code="max_uses_exceeded"),
            )
        ]
    )
    r = await s.search("x")  # billed by the provider, so returned (and recorded), not raised
    assert r.error == "search_max_uses_exceeded" and r.hits == () and r.input_tokens == 1000


async def test_a_billed_search_error_is_recorded_and_refused_to_the_agent() -> None:
    s, _ = provider(
        [SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(error_code="x"))]
    )
    seen: list[SearchResult] = []

    async def record(r: SearchResult) -> None:
        seen.append(r)

    tool = build_search_tool(s, max_calls=3, on_result=record)
    assert (await tool.ainvoke({"query": "q"})) == "REFUSED: search_x"
    assert len(seen) == 1 and seen[0].error == "search_x"


async def test_searches_stop_once_the_phase_cost_budget_is_spent() -> None:
    state = {"ok": True}
    searcher = FakeSearcher()
    tool = build_search_tool(searcher, max_calls=5, allowed=lambda: state["ok"])
    await tool.ainvoke({"query": "a"})
    state["ok"] = False
    out = await tool.ainvoke({"query": "b"})
    assert out.startswith("REFUSED: phase_cost_budget_exhausted") and searcher.queries == ["a"]


async def test_search_hits_on_unlisted_subdomains_or_hosts_are_dropped() -> None:
    messages = FakeMessages(
        [result_block(
            "https://docs.python.org/3/x.html", "https://wiki.docs.python.org/x",
            "https://docs.python.org.evil.example/x", "http://docs.python.org/3/y.html",
        )]
    )  # fmt: skip
    s = AnthropicDocumentSearch(
        api_key="k", model="m", allowed_hosts=["docs.python.org"],
        client=SimpleNamespace(messages=messages),
    )  # fmt: skip
    r = await s.search("x")
    assert [h.url for h in r.hits] == ["https://docs.python.org/3/x.html"]


class FakeSearcher:
    allowed_hosts = ("docs.python.org",)

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str) -> SearchResult:
        self.queries.append(query)
        return SearchResult(
            query, (SearchHit("Asyncio", "https://docs.python.org/3/library/asyncio.html"),),
            "2026-11-26T10:00:00+00:00", "fake",
        )  # fmt: skip


async def test_the_tool_refuses_secrets_enforces_its_budget_and_reports_provenance() -> None:
    searcher, seen = FakeSearcher(), []

    async def on_result(r: SearchResult) -> None:
        seen.append(r)

    tool = build_search_tool(searcher, max_calls=1, on_result=on_result)
    refused = await tool.ainvoke({"query": "token ghp_0123456789abcdefghijklmnopqrstuvwxyz"})
    assert refused.startswith("REFUSED: search_query_contains_secret") and searcher.queries == []
    out = await tool.ainvoke({"query": "asyncio taskgroup"})
    assert "UNTRUSTED" in out and "retrieved 2026-11-26" in out and "docs.python.org" in out
    assert len(seen) == 1
    assert (await tool.ainvoke({"query": "again"})).startswith("REFUSED: search_budget_exhausted")


async def test_official_documentation_ranks_before_registries_and_raw_sources() -> None:
    messages = FakeMessages(
        [result_block("https://pypi.org/project/x/", "https://docs.python.org/3/x.html")]
    )
    s = AnthropicDocumentSearch(
        api_key="k", model="m", allowed_hosts=["pypi.org", "docs.python.org"],
        client=SimpleNamespace(messages=messages),
    )  # fmt: skip
    r = await s.search("x")
    assert r.hits[0].url == "https://docs.python.org/3/x.html"


async def test_search_hits_with_a_query_string_or_an_unlisted_host_are_not_returned() -> None:
    messages = FakeMessages(
        [result_block(
            "https://docs.python.org/3/search.html?q=asyncio",
            "https://docs.python.org/3/library/asyncio.html?highlight=taskgroup",
            "https://evil.example/docs.python.org/3/",
            "https://fastapi.tiangolo.com/tutorial/#first-steps",
            "https://docs.python.org/3/library/asyncio-task.html",
        )]
    )  # fmt: skip
    s = AnthropicDocumentSearch(
        api_key="k", model="m", allowed_hosts=["docs.python.org", "fastapi.tiangolo.com"],
        client=SimpleNamespace(messages=messages),
    )  # fmt: skip
    r = await s.search("asyncio taskgroup")
    # fetch_reference refuses any agent URL with a query string, so such hits are never offered
    assert [h.url for h in r.hits] == [
        "https://fastapi.tiangolo.com/tutorial/#first-steps",
        "https://docs.python.org/3/library/asyncio-task.html",
    ]
