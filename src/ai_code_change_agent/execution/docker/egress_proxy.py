"""Allow-list egress proxy for the dependency resolver container (ADR-010).

Runs as its own short-lived container (standard library only) attached to two networks: the
`--internal` resolver network (no route anywhere) and the default bridge. The resolver container
is attached ONLY to the internal network, so this proxy is its single path out. The proxy:

- accepts only `CONNECT host:443` (TLS stays end to end; pip verifies the index certificate);
- accepts only host names on the allow-list (exact, case-insensitive), never IP literals;
- resolves the name itself and refuses it if ANY address is not globally routable (loopback,
  private, link-local such as 169.254.169.254, the Docker host gateway, multicast, reserved), so DNS
  cannot be used to reach internal services or cloud metadata;
- exits on its own after --lifetime seconds, so a crashed worker never leaves it running.

Usage: python egress_proxy.py --allow pypi.org --allow files.pythonhosted.org [--port 3128]
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import re
import socket
import sys

_HOST = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_MAX_HEAD = 8192
_IDLE = 120.0


def log(*parts: object) -> None:
    print("egress", *parts, file=sys.stderr, flush=True)


def decide(target: str, allow: frozenset[str]) -> tuple[str, int] | str:
    """Return (host, port) when the CONNECT target is allowed, else the refusal reason."""
    host, sep, port = target.rpartition(":")
    host = host.lower().rstrip(".")
    if not sep or port != "443":
        return "port_not_allowed"
    if not _HOST.match(host):
        return "host_invalid"
    if host not in allow:
        return "host_not_allowed"
    return host, 443


def global_addresses(host: str) -> list[str] | str:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return "dns_failed"
    addrs = sorted({str(i[4][0]) for i in infos})
    if not addrs:
        return "dns_failed"
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            return "address_not_public"
    return addrs


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await asyncio.wait_for(reader.read(65536), _IDLE):
            writer.write(data)
            await writer.drain()
    except (OSError, TimeoutError):
        pass
    finally:
        writer.close()


async def handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, allow: frozenset[str]
) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError, TimeoutError):
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    verdict: tuple[str, int] | str = "method_not_allowed"
    if len(parts) == 3 and parts[0] == "CONNECT":
        verdict = decide(parts[1], allow)
    if isinstance(verdict, tuple):
        addrs = await asyncio.to_thread(global_addresses, verdict[0])
        if isinstance(addrs, str):
            verdict = addrs
    if isinstance(verdict, str):
        log("deny", verdict, line[:200])
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(addrs[0], 443), 15)
    except (OSError, TimeoutError):
        log("fail", verdict[0])
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        writer.close()
        return
    log("allow", verdict[0])
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))


async def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--allow", action="append", default=[])
    p.add_argument("--port", type=int, default=3128)
    p.add_argument("--lifetime", type=float, default=3600.0)
    a = p.parse_args(argv)
    allow = frozenset(h.lower().rstrip(".") for h in a.allow)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, allow),
        "0.0.0.0",  # noqa: S104 - container-internal listener on the resolver network
        a.port,
        limit=_MAX_HEAD,
    )
    log("ready", ",".join(sorted(allow)))
    async with server:
        try:
            await asyncio.wait_for(server.serve_forever(), a.lifetime)
        except TimeoutError:
            log("lifetime_reached")


if __name__ == "__main__":
    asyncio.run(main())
