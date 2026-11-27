"""Port for reading reference documentation from the web on the agent's behalf.

Like a harness-side web fetch: the request is made by trusted infrastructure under permission
rules (exact trusted hosts from config/reference_sources.yaml, public addresses only); the coding
sandbox never gets network."""

from dataclasses import dataclass
from typing import Protocol


class ReferenceRefusedError(Exception):
    """The URL or the response violates the reference policy. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReferenceDocument:
    url: str  # the final URL after allowed redirects
    content_type: str
    text: str
    truncated: bool
    retrieved_at: str = ""  # ISO 8601, UTC


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    page_age: str | None = None


@dataclass(frozen=True)
class SearchResult:
    """Search results from trusted infrastructure (never from the sandbox), with provenance."""

    query: str
    hits: tuple[SearchHit, ...]
    retrieved_at: str  # ISO 8601, UTC
    provider: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    searches: int = 0
    error: str | None = None  # the provider ran (and billed) the request but returned an error


class ReferenceSearcher(Protocol):
    """Provider-independent documentation search; results are untrusted reference data."""

    @property
    def allowed_hosts(self) -> tuple[str, ...]: ...

    async def search(self, query: str) -> SearchResult: ...


class ReferenceFetcher(Protocol):
    @property
    def allowed_hosts(self) -> tuple[str, ...]: ...

    async def fetch(self, url: str) -> ReferenceDocument: ...
