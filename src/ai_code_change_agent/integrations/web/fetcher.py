"""Harness-side reference fetcher (the platform's equivalent of a web-fetch tool).

Permission rules, all enforced here and none by the model:
- https only, default port, no credentials in the URL;
- the host must equal or be a subdomain of an allow-listed domain (REFERENCE_ALLOWED_DOMAINS);
- every address the host resolves to must be public (no private, loopback, link-local,
  multicast or reserved ranges: no SSRF into the host or internal network);
- redirects are followed manually, at most three, and each hop is checked again;
- GET only, no cookies, no authorization headers, no proxy settings from the environment;
- text-like content types only, a byte cap while streaming, HTML reduced to text.
A small DNS-rebinding window remains between the check and the connection; the allow-list keeps
it to the listed sites.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from ai_code_change_agent.application.references.ports import (
    ReferenceDocument,
    ReferenceRefusedError,
)
from ai_code_change_agent.core.logging import get_logger

Resolver = Callable[[str], Awaitable[list[str]]]
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_MAX_REDIRECTS = 3
_log = get_logger("references")


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class _Text(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "svg", "head"})
    _BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BREAK:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            href = dict(attrs).get("href")
            if href and href.startswith(("http", "/")):
                self.parts.append(f" [{href}] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


class HttpReferenceFetcher:
    def __init__(
        self,
        *,
        allowed_domains: Sequence[str],
        max_bytes: int = 1_048_576,
        max_chars: int = 40_000,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = _resolve,
    ) -> None:
        self._domains = tuple(sorted({d.lower().strip(".") for d in allowed_domains if d}))
        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._timeout = timeout_seconds
        self._transport = transport
        self._resolve = resolver

    def __repr__(self) -> str:
        return f"HttpReferenceFetcher(domains={self._domains!r})"

    @property
    def allowed_domains(self) -> tuple[str, ...]:
        return self._domains

    def _allowed_host(self, host: str) -> bool:
        return any(host == d or host.endswith("." + d) for d in self._domains)

    async def _check(self, url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host or parts.username or parts.password:
            raise ReferenceRefusedError("reference_url_not_allowed")
        if parts.port not in (None, 443):
            raise ReferenceRefusedError("reference_url_not_allowed")
        if not self._allowed_host(host):
            raise ReferenceRefusedError("reference_domain_not_allowed")
        try:
            addresses = await self._resolve(host)
        except OSError:
            raise ReferenceRefusedError("reference_unresolvable") from None
        if not addresses:
            raise ReferenceRefusedError("reference_unresolvable")
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%")[0])
            if not ip.is_global or ip.is_multicast:
                raise ReferenceRefusedError("reference_address_not_public")
        return url

    async def fetch(self, url: str) -> ReferenceDocument:
        current = await self._check(url.strip())
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={"User-Agent": "ai-code-change-agent-reference-fetcher", "Accept": "*/*"},
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            current = await self._check(urljoin(current, location))
                            continue
                        return await self._read(current, response)
                except httpx.HTTPError:
                    raise ReferenceRefusedError("reference_unavailable") from None
        raise ReferenceRefusedError("reference_too_many_redirects")

    async def _read(self, url: str, response: httpx.Response) -> ReferenceDocument:
        if response.status_code != 200:
            raise ReferenceRefusedError(f"reference_http_{response.status_code}")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith(_TEXT_TYPES):
            raise ReferenceRefusedError("reference_content_type_not_text")
        data = bytearray()
        truncated = False
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > self._max_bytes:
                del data[self._max_bytes :]
                truncated = True
                break
        text = data.decode(response.encoding or "utf-8", errors="replace")
        if "html" in content_type:
            text = html_to_text(text)
        if len(text) > self._max_chars:
            text, truncated = text[: self._max_chars], True
        _log.info(
            "reference_fetched",
            host=urlsplit(url).hostname,
            status=response.status_code,
            chars=len(text),
            truncated=truncated,
        )
        return ReferenceDocument(url, content_type, text, truncated)
