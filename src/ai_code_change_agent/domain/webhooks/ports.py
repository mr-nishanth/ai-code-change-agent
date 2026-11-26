"""Ports. Deployment-specific verifiers and storage adapters implement these."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ai_code_change_agent.domain.webhooks.models import Outcome, ReceivedDelivery, RejectionCode


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    failure: RejectionCode | None = None


class WebhookVerifier(Protocol):
    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerificationResult:
        """Authenticate the exact raw request bytes. Header names arrive lower-cased."""
        ...


class WebhookRepository(Protocol):
    async def record(self, delivery: ReceivedDelivery) -> Outcome:
        """Atomically record the delivery (and, if accepted and new, request + outbox)."""
        ...
