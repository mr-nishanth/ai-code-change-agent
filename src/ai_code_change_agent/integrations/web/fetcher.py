"""Harness-side reference fetcher (the platform's equivalent of a web-fetch tool).

Permission rules, all enforced here and none by the model:
- https only, default port, no credentials in the URL;
- the host must exactly equal a trusted host (config/reference_sources.yaml, see
  core.reference_hosts); any path on it is allowed, subdomains are not implied;
- every address the host resolves to must be public (no private, loopback, link-local,
  multicast or reserved ranges: no SSRF into the host or internal network); IPv6 forms that
  embed or stand for an IPv4 address (IPv4-compatible ::/96, SIIT ::ffff:0:0:0/96) and
  site-local fec0::/10 are refused outright, and NAT64 64:ff9b::/96 only reaches a public IPv4;
- redirects are followed manually, at most three, and each hop is checked again (scheme, port,
  exact host, public addresses), so a redirect to another host needs that host listed too;
- a URL the agent asks for has no query string, at most MAX_REQUESTED_URL characters and no
  secret (server redirects may carry a query; they come from the listed site, not the agent);
- GET only, no cookies, no authorization headers, no proxy settings from the environment;
- text-like content types only, a byte cap while streaming, HTML reduced to text.
A small DNS-rebinding window remains between the check and the connection; the allow-list keeps
it to the listed sites.
"""

import asyncio
import codecs
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ai_code_change_agent.application.artifacts.redaction import find_secrets
from ai_code_change_agent.application.references.ports import (
    ReferenceDocument,
    ReferenceRefusedError,
)
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.core.reference_hosts import host_allowed, normalize_host

Resolver = Callable[[str], Awaitable[list[str]]]
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_MAX_REDIRECTS = 3
MAX_REQUESTED_URL = 300  # characters, for a URL the agent asks for
# Python's is_global misses these (they report global on 3.12); listed explicitly so the result
# does not depend on the stdlib patch level.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_REFUSED_V6 = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::/96",  # IPv4-compatible (deprecated)
        "::ffff:0:0:0/96",  # SIIT IPv4-translated
        "fec0::/10",  # site-local (deprecated)
    )
)
_log = get_logger("references")


def address_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when the fetcher may connect to `ip`. The stdlib check is the base rule; the IPv6
    rules only refuse more. A NAT64 address is allowed only when its embedded IPv4 is public, so
    hosts on IPv6-only DNS64 networks still work."""
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            return embedded.is_global and not embedded.is_multicast
        if any(ip in net for net in _REFUSED_V6):
            return False
    return True


def url_refusal(url: str, hosts: Sequence[str], *, requested: bool = True) -> str | None:
    """The static (no DNS) part of the URL check: the refusal code, or None when the URL passes.
    `requested`: the URL comes from the agent (not a server redirect), so it also may not carry
    a query string, be long, or contain a secret. `hosts` are normalised trusted hosts. Raises
    ValueError for a URL urlsplit cannot parse (for example a non-numeric port)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if requested:
        if parts.query:
            return "reference_query_not_allowed"
        if len(url) > MAX_REQUESTED_URL:
            return "reference_url_too_long"
        if find_secrets(url):
            return "reference_url_contains_secret"
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return "reference_url_not_allowed"
    if parts.port not in (None, 443):
        return "reference_url_not_allowed"
    if not host_allowed(host, hosts):
        return "reference_domain_not_allowed"
    return None


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
        allowed_hosts: Sequence[str],
        max_bytes: int = 1_048_576,
        max_chars: int = 40_000,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = _resolve,
    ) -> None:
        self._hosts = tuple(sorted({normalize_host(d) for d in allowed_hosts if d}))
        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._timeout = timeout_seconds
        self._transport = transport
        self._resolve = resolver

    def __repr__(self) -> str:
        return f"HttpReferenceFetcher(domains={self._hosts!r})"

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return self._hosts

    async def _check(self, url: str, *, requested: bool = False) -> str:
        """`requested`: the URL comes from the agent (not a server redirect). Such a URL may not
        carry a query string, may not be long, and may not contain a secret, so Story text,
        organisation details or repository content cannot be sent to a site in a URL, whatever
        a fetched page (an llms.txt, for example) asks for. Path text remains a small channel;
        the length cap bounds it and every requested URL is logged."""
        code = url_refusal(url, self._hosts, requested=requested)
        if code is not None:
            raise ReferenceRefusedError(code)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if requested:
            url = urlunsplit(parts._replace(fragment=""))
        try:
            addresses = await self._resolve(host)
        except OSError:
            raise ReferenceRefusedError("reference_unresolvable") from None
        if not addresses:
            raise ReferenceRefusedError("reference_unresolvable")
        for address in addresses:
            if not address_is_public(ipaddress.ip_address(address.split("%")[0])):
                raise ReferenceRefusedError("reference_address_not_public")
        return url

    async def fetch(self, url: str) -> ReferenceDocument:
        current = await self._check(url.strip(), requested=True)
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
                            target = urljoin(current, response.headers.get("location", ""))
                            try:
                                current = await self._check(target)
                            except ReferenceRefusedError as exc:
                                host = (urlsplit(target).hostname or "?")[:100]
                                _log.info("reference_redirect_refused", to_host=host, code=exc.code)
                                raise ReferenceRefusedError(
                                    f"reference_redirect_not_allowed:{host}"
                                ) from None
                            continue
                        return await self._read(current, response)
                except httpx.HTTPError:
                    raise ReferenceRefusedError("reference_unavailable") from None
        raise ReferenceRefusedError("reference_too_many_redirects")

    async def _read_html(self, response: httpx.Response) -> tuple[str, bool]:
        """Stream HTML through the text extractor, keeping only extracted text in memory. Many
        documentation pages carry megabytes of inline scripts before <body>; the raw stream is
        bounded at 8x REFERENCE_MAX_BYTES, the extracted text at max_chars."""
        raw_cap = self._max_bytes * 8
        decoder = codecs.getincrementaldecoder(response.encoding or "utf-8")(errors="replace")
        parser = _Text()
        seen = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            seen += len(chunk)
            parser.feed(decoder.decode(chunk))
            if seen > raw_cap or sum(map(len, parser.parts)) > self._max_chars * 2:
                truncated = True
                break
        else:
            parser.feed(decoder.decode(b"", final=True))
        parser.close()
        lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
        return "\n".join(line for line in lines if line), truncated

    async def _read(self, url: str, response: httpx.Response) -> ReferenceDocument:
        if response.status_code != 200:
            raise ReferenceRefusedError(f"reference_http_{response.status_code}")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith(_TEXT_TYPES):
            raise ReferenceRefusedError("reference_content_type_not_text")
        truncated = False
        if "html" in content_type:
            text, truncated = await self._read_html(response)
        else:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self._max_bytes:
                    del data[self._max_bytes :]
                    truncated = True
                    break
            text = data.decode(response.encoding or "utf-8", errors="replace")
        if len(text) > self._max_chars:
            text, truncated = text[: self._max_chars], True
        _log.info(
            "reference_fetched",
            host=urlsplit(url).hostname,
            status=response.status_code,
            chars=len(text),
            truncated=truncated,
        )
        return ReferenceDocument(
            url, content_type, text, truncated, datetime.now(UTC).isoformat(timespec="seconds")
        )
