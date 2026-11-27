"""Documentation search through Anthropic's server-side web search tool (harness side).

The worker asks the Messages API to run one web search restricted to the trusted official
documentation hosts (config/reference_sources.yaml) and returns only the result list (title, URL,
page age). Nothing from the sandbox is sent: the query comes from the agent's tool call and is
refused when it looks like it contains a secret. Results are filtered again by exact host (defence
in depth) and are
untrusted data; pages are read with fetch_reference, whose own SSRF checks apply.

Verified 2026-11-26 against claude-haiku-5-5 with tool type web_search_20250305 (anthropic SDK
2.14.0); see https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool ."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import anthropic

from ai_code_change_agent.application.references.ports import (
    ReferenceRefusedError,
    SearchHit,
    SearchResult,
)
from ai_code_change_agent.core.reference_hosts import host_allowed, normalize_host

TOOL_TYPE = "web_search_20250305"
MAX_QUERY = 300
MAX_HITS = 8
# Package registries and raw source hosts can be allow-listed for version facts, but official
# documentation sites rank first.
SECONDARY_HOSTS = ("pypi.org", "npmjs.com", "raw.githubusercontent.com", "github.com")


def _secondary(url: str) -> bool:
    """Ranking only (not authorisation): registries and raw source hosts sort after docs."""
    host = url.split("://", 1)[-1].split("/", 1)[0].lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in SECONDARY_HOSTS)


class AnthropicDocumentSearch:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        allowed_hosts: Sequence[str],
        timeout_seconds: float = 60,
        client: Any = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts is required")
        self._hosts = tuple(sorted({normalize_host(d) for d in allowed_hosts}))
        self._model = model
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=2
        )

    def __repr__(self) -> str:  # never shows the key
        return f"AnthropicDocumentSearch(model={self._model!r}, domains={len(self._hosts)})"

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return self._hosts

    async def search(self, query: str) -> SearchResult:
        query = " ".join(query.split())[:MAX_QUERY]
        if not query:
            raise ReferenceRefusedError("search_query_empty")
        tool: Any = {
            "type": TOOL_TYPE,
            "name": "web_search",
            "max_uses": 1,
            "allowed_domains": list(self._hosts),
        }
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                tools=[tool],
                messages=[
                    {
                        "role": "user",
                        "content": "Run one web search for this documentation query and stop; "
                        f"do not answer it yourself.\nQuery: {query}",
                    }
                ],
            )
        except anthropic.APIStatusError as exc:
            raise ReferenceRefusedError(
                "search_rate_limited" if exc.status_code == 429 else "search_unavailable"
            ) from None
        except anthropic.APIError:
            raise ReferenceRefusedError("search_unavailable") from None
        hits: list[SearchHit] = []
        error: str | None = None
        blocks: list[Any] = list(response.content)
        for block in blocks:
            if getattr(block, "type", None) != "web_search_tool_result":
                continue
            content = block.content
            if not isinstance(content, list):  # an error object: billed, so still recorded
                error = f"search_{getattr(content, 'error_code', 'error')}"
                continue
            for item in content:
                url = str(getattr(item, "url", ""))
                host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
                fresh = url not in {h.url for h in hits}
                # fetch_reference refuses an agent URL with a query string: never offer one
                usable = url.startswith("https://") and not urlsplit(url).query
                if usable and host_allowed(host, self._hosts) and fresh:
                    hits.append(
                        SearchHit(
                            str(getattr(item, "title", ""))[:200],
                            url[:500],
                            getattr(item, "page_age", None),
                        )
                    )
        hits.sort(key=lambda h: _secondary(h.url))  # stable: provider order within each group
        usage = response.usage
        server = getattr(usage, "server_tool_use", None)
        return SearchResult(
            query=query,
            hits=tuple(hits[:MAX_HITS]),
            retrieved_at=datetime.now(UTC).isoformat(timespec="seconds"),
            provider="anthropic-web-search",
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            searches=int(getattr(server, "web_search_requests", 0) or 0),
            error=error,
        )
