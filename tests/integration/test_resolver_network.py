# ruff: noqa: S607, ASYNC221 - test drives the docker CLI
"""Real Docker: the resolver's only way out is the allow-list egress proxy (ADR-010).

A container on the resolver route has no direct route to the internet, the Docker host
(host.docker.internal, where Postgres/the API listen), or cloud metadata (169.254.169.254). Through
the proxy, only CONNECT to the index hosts on 443 succeeds; other hosts, IP literals, plain HTTP and
names resolving to internal addresses are refused.
"""

import os
import shutil
import subprocess

import pytest

from ai_code_change_agent.execution.docker.process import SubprocessRunner
from ai_code_change_agent.execution.docker.resolver_network import ResolverEgress, egress_hosts

pytestmark = pytest.mark.docker
IMAGE = os.environ.get("WORKSPACE_IMAGE", "ai-code-change-agent/execution-python:0.2.0")
NETWORK = "aica-test-dep-resolver"

PROBE = r"""
import os, socket, urllib.request
def t(label, f):
    try:
        f(); print(label, "OPEN")
    except Exception:
        print(label, "BLOCKED")
t("direct_ip", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3))
t("direct_metadata", lambda: socket.create_connection(("169.254.169.254", 80), timeout=3))
t("direct_host", lambda: socket.create_connection(("host.docker.internal", 5432), timeout=3))
t("direct_pypi", lambda: socket.create_connection(("pypi.org", 443), timeout=3))
P = os.environ["HTTPS_PROXY"]
op = urllib.request.build_opener(urllib.request.ProxyHandler({"https": P, "http": P}))
t("proxy_index", lambda: op.open("https://pypi.org/simple/six/", timeout=15).read(1))
t("proxy_other_host", lambda: op.open("https://example.com/", timeout=10))
t("proxy_ip_literal", lambda: op.open("https://1.1.1.1/", timeout=10))
t("proxy_metadata", lambda: op.open("http://169.254.169.254/latest/meta-data/", timeout=10))
t("proxy_plain_http", lambda: op.open("http://pypi.org/simple/", timeout=10))
"""


def _ok(*argv: str) -> bool:
    return subprocess.run(argv, capture_output=True, check=False, timeout=60).returncode == 0  # noqa: S603


@pytest.fixture(scope="module", autouse=True)
def _requirements() -> None:
    if shutil.which("docker") is None or not _ok("docker", "info"):
        pytest.skip("docker daemon not reachable")
    if not _ok("docker", "image", "inspect", IMAGE):
        pytest.skip(f"execution image '{IMAGE}' is not built (see docs/GETTING-STARTED.md)")


async def test_resolver_route_allows_only_the_index_through_the_proxy() -> None:
    egress = ResolverEgress(
        runner=SubprocessRunner(), image=IMAGE, allow_hosts=egress_hosts("https://pypi.org/simple"),
        network=NETWORK,
    )  # fmt: skip
    async with egress.route() as route:
        proxy = route.proxy_url.split("//")[1].split(":")[0]
        out = subprocess.run(  # noqa: S603
            ["docker", "run", "--rm", "--pull", "never", "--network", route.network,
             "--env", f"HTTPS_PROXY={route.proxy_url}", IMAGE, "python", "-c", PROBE],
            capture_output=True, text=True, check=False, timeout=120,
        )  # fmt: skip
        result = dict(line.split() for line in out.stdout.split("\n") if line.strip())
    assert result == {
        "direct_ip": "BLOCKED",
        "direct_metadata": "BLOCKED",
        "direct_host": "BLOCKED",
        "direct_pypi": "BLOCKED",
        "proxy_index": "OPEN" if _index_reachable() else "BLOCKED",
        "proxy_other_host": "BLOCKED",
        "proxy_ip_literal": "BLOCKED",
        "proxy_metadata": "BLOCKED",
        "proxy_plain_http": "BLOCKED",
    }, out.stderr[-2000:]
    inspect = subprocess.run(  # noqa: S603
        ["docker", "network", "inspect", "--format", "{{.Internal}}", NETWORK],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert inspect.stdout.strip() == "true"
    assert not _ok("docker", "container", "inspect", proxy)  # proxy removed after use


def _index_reachable() -> bool:
    try:
        import httpx

        httpx.get("https://pypi.org/simple/six/", timeout=10).raise_for_status()
    except Exception:
        return False
    return True
