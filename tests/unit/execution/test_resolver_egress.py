"""Resolver egress boundary: allow-list decisions, address filtering and network checks."""

from collections.abc import Sequence

import pytest

from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError
from ai_code_change_agent.execution.docker import egress_proxy
from ai_code_change_agent.execution.docker.process import ProcessResult
from ai_code_change_agent.execution.docker.resolver_network import ResolverEgress, egress_hosts

ALLOW = frozenset({"pypi.org", "files.pythonhosted.org"})


def test_egress_hosts_are_the_index_host_plus_the_pypi_file_host() -> None:
    assert egress_hosts("https://pypi.org/simple") == ALLOW
    assert egress_hosts("https://pkgs.example.com/simple") == {"pkgs.example.com"}


@pytest.mark.parametrize(
    ("target", "verdict"),
    [
        ("pypi.org:443", ("pypi.org", 443)),
        ("PyPI.org.:443", ("pypi.org", 443)),
        ("pypi.org:80", "port_not_allowed"),
        ("example.com:443", "host_not_allowed"),
        ("169.254.169.254:443", "host_not_allowed"),
        ("pypi.org", "port_not_allowed"),
        ("bad_host!:443", "host_invalid"),
    ],
)
def test_proxy_decisions(target: str, verdict: object) -> None:
    assert egress_proxy.decide(target, ALLOW) == verdict


def test_names_resolving_to_internal_addresses_are_refused() -> None:
    assert egress_proxy.global_addresses("localhost") == "address_not_public"
    assert egress_proxy.global_addresses("nonexistent.invalid") == "dns_failed"


class Docker:
    def __init__(self, internal: str = "true", inspect_code: int = 0, run_code: int = 0) -> None:
        self.internal, self.inspect_code, self.run_code = internal, inspect_code, run_code
        self.calls: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        argv = list(argv)
        self.calls.append(argv)
        out, code = b"", 0
        if argv[1:3] == ["network", "inspect"]:
            out, code = self.internal.encode(), self.inspect_code
        elif argv[1] == "run":
            code = self.run_code
        elif argv[1] == "logs":
            out = b"egress ready pypi.org"
        return ProcessResult(
            exit_code=code, stdout=out, stderr=b"", truncated=False, timed_out=False
        )


async def test_route_runs_a_hardened_proxy_and_always_removes_it() -> None:
    docker = Docker()
    egress = ResolverEgress(runner=docker, image="img", allow_hosts=ALLOW, network="n")
    async with egress.route() as route:
        assert route.network == "n" and route.proxy_url.startswith("http://aica-egress-")
    run = next(c for c in docker.calls if c[1] == "run")
    for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--lifetime"):
        assert flag in run
    assert run[run.index("--network") + 1] == "n"
    assert run[-4:] == ["--allow", "files.pythonhosted.org", "--allow", "pypi.org"]
    assert any(c[1:4] == ["network", "connect", "bridge"] for c in docker.calls)
    assert docker.calls[-1][1:3] == ["rm", "-f"]


async def test_an_existing_routable_network_is_never_used() -> None:
    egress = ResolverEgress(runner=Docker(internal="false"), image="img", allow_hosts=ALLOW)
    with pytest.raises(DependencyPolicyError, match="dependency_preparation_failed"):
        async with egress.route():
            pass


async def test_a_missing_network_is_created_internal_and_proxy_failure_fails_closed() -> None:
    docker = Docker(inspect_code=1, run_code=125)
    egress = ResolverEgress(runner=docker, image="img", allow_hosts=ALLOW)
    with pytest.raises(DependencyPolicyError, match="dependency_preparation_failed"):
        async with egress.route():
            pass
    create = next(c for c in docker.calls if c[1:3] == ["network", "create"])
    assert "--internal" in create


def test_an_empty_allow_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="allowed host"):
        ResolverEgress(runner=Docker(), image="img", allow_hosts=frozenset())


class PsDocker(Docker):
    def __init__(self, listing: str) -> None:
        super().__init__()
        self.listing = listing

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        if list(argv)[1] == "ps":
            self.calls.append(list(argv))
            return ProcessResult(
                exit_code=0, stdout=self.listing.encode(), stderr=b"", truncated=False,
                timed_out=False,
            )  # fmt: skip
        return await super().run(argv, deadline=deadline, max_output=max_output)


async def test_only_proxies_of_dead_local_workers_are_reaped() -> None:
    import os

    dead = 2**22 + 12345  # beyond any pid in use
    listing = (
        f"aica-egress-dead {dead}\naica-egress-live {os.getppid()}\n"
        f"aica-egress-mine {os.getpid()}\nother-container {dead}\naica-egress-bad x\n"
    )
    docker = PsDocker(listing)
    egress = ResolverEgress(runner=docker, image="img", allow_hosts=ALLOW)
    assert await egress.reap_orphans() == ["aica-egress-dead"]
    ps = docker.calls[0]
    assert "label=aica.role=egress" in ps and any(
        a.startswith("label=aica.owner.host=") for a in ps
    )
    assert [c for c in docker.calls if c[1] == "rm"] == [["docker", "rm", "-f", "aica-egress-dead"]]
