"""Network boundary of the dependency resolver container (ADR-010).

The resolver is attached ONLY to a Docker `--internal` network, which has no route to the internet,
the host (host.docker.internal), cloud metadata (169.254.169.254) or any other network. Its single
way out is a short-lived allow-list proxy container (`egress_proxy.py`) attached to that network
and to the default bridge. The proxy permits `CONNECT` to the package index host names only, on port
443, and refuses names that resolve to non-public addresses. This is enforced by Docker networking,
not by pip arguments: a process in the resolver that ignores the proxy has no route at all.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError
from ai_code_change_agent.execution.docker.process import ProcessRunner

_log = get_logger("resolver_network")

DEFAULT_RESOLVER_NETWORK = "aica-dep-resolver"
PROXY_SCRIPT = Path(__file__).with_name("egress_proxy.py")
_PORT = 3128


@dataclass(frozen=True)
class ResolverRoute:
    """Where the resolver container runs and the proxy it must use."""

    network: str
    proxy_url: str


def egress_hosts(index_url: str, extra: Iterable[str] = ()) -> frozenset[str]:
    """Host names the resolver may reach: the index host (plus PyPI's file host for PyPI)."""
    host = (urlsplit(index_url).hostname or "").lower()
    hosts = {host, *(h.strip().lower() for h in extra if h.strip())}
    if host == "pypi.org":
        hosts.add("files.pythonhosted.org")
    hosts.discard("")
    return frozenset(hosts)


class ResolverEgress:
    """Creates the internal network (idempotent) and one proxy container per preparation."""

    def __init__(
        self,
        *,
        runner: ProcessRunner,
        image: str,
        allow_hosts: frozenset[str],
        network: str = DEFAULT_RESOLVER_NETWORK,
        docker: str = "docker",
        lifetime_seconds: float = 3600.0,
    ) -> None:
        if not allow_hosts:
            raise ValueError("resolver egress needs at least one allowed host")
        self._runner = runner
        self._image = image
        self._allow = allow_hosts
        self._network = network
        self._docker = docker
        self._lifetime = lifetime_seconds

    async def _docker_run(self, *args: str, deadline: float = 60.0) -> tuple[int, str]:
        try:
            r = await self._runner.run([self._docker, *args], deadline=deadline, max_output=65536)
        except OSError:
            raise DependencyPolicyError("dependency_preparation_failed") from None
        out = ((r.stdout or b"") + (r.stderr or b"")).decode(errors="replace")
        code = 124 if r.timed_out or r.exit_code is None else r.exit_code
        return code, out

    async def _ensure_network(self) -> None:
        code, out = await self._docker_run(
            "network", "inspect", "--format", "{{.Internal}}", self._network
        )
        if code == 0:
            if out.strip() != "true":  # never run the resolver on a routable network
                _log.error("resolver_network_not_internal", network=self._network)
                raise DependencyPolicyError("dependency_preparation_failed")
            return
        code, out = await self._docker_run(
            "network", "create", "--driver", "bridge", "--internal",
            "--label", "aica.managed=true", self._network,
        )  # fmt: skip
        if code != 0 and "already exists" not in out:
            _log.error("resolver_network_create_failed", output=out[-500:])
            raise DependencyPolicyError("dependency_preparation_failed")

    @asynccontextmanager
    async def route(self) -> AsyncIterator[ResolverRoute]:
        await self._ensure_network()
        name = f"aica-egress-{uuid.uuid4().hex[:10]}"
        allow = [arg for h in sorted(self._allow) for arg in ("--allow", h)]
        code, out = await self._docker_run(
            "run", "-d", "--rm", "--pull", "never", "--name", name,
            "--label", "aica.managed=true", "--label", "aica.role=egress",
            "--network", self._network,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "64", "--memory", "128m", "--user", "65534:65534",
            "--mount", f"type=bind,src={PROXY_SCRIPT},dst=/proxy/egress_proxy.py,readonly",
            self._image, "python", "-I", "/proxy/egress_proxy.py", "--port", str(_PORT),
            "--lifetime", str(int(self._lifetime)), *allow,
        )  # fmt: skip
        if code != 0:
            _log.error("egress_proxy_start_failed", output=out[-500:])
            raise DependencyPolicyError("dependency_preparation_failed")
        try:
            code, out = await self._docker_run("network", "connect", "bridge", name)
            if code != 0:
                _log.error("egress_proxy_connect_failed", output=out[-500:])
                raise DependencyPolicyError("dependency_preparation_failed")
            await self._wait_ready(name)
            yield ResolverRoute(self._network, f"http://{name}:{_PORT}")
        finally:
            await self._docker_run("rm", "-f", name)

    async def _wait_ready(self, name: str) -> None:
        for _ in range(50):
            code, out = await self._docker_run("logs", name, deadline=10.0)
            if code == 0 and "egress ready" in out:
                return
            await asyncio.sleep(0.2)
        raise DependencyPolicyError("dependency_preparation_failed")
