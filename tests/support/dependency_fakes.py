"""Test doubles for the resolver egress route and the index metadata verifier."""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from ai_code_change_agent.application.dependencies.ports import LockedPackage
from ai_code_change_agent.execution.docker.metadata_verifier import WheelVerification
from ai_code_change_agent.execution.docker.resolver_network import ResolverRoute

TEST_NETWORK = "aica-test-resolver"
TEST_PROXY = "http://aica-egress-test:3128"


class StaticEgress:
    """Yields a fixed route; records how many routes were opened."""

    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def route(self) -> AsyncIterator[ResolverRoute]:
        self.opened += 1
        yield ResolverRoute(TEST_NETWORK, TEST_PROXY)


class PublishedVerifier:
    """Acts as an index that publishes exactly the given hashes (default: whatever was hashed)."""

    def __init__(self, published: dict[str, str] | None = None) -> None:
        self.published = published
        self.calls = 0

    def verify(self, packages: Iterable[LockedPackage]) -> tuple[WheelVerification, ...]:
        from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError

        self.calls += 1
        out = []
        for p in packages:
            pub = p.sha256 if self.published is None else self.published.get(p.filename)
            if pub is None:
                raise DependencyPolicyError("dependency_metadata_unavailable")
            if pub != p.sha256:
                raise DependencyPolicyError("dependency_hash_mismatch")
            out.append(WheelVerification(p.filename, p.sha256, pub, "pep691_json"))
        return tuple(out)
