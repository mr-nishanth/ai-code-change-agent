"""In-memory WebhookRepository double with the same duplicate semantics as the DB adapter."""

from ai_code_change_agent.domain.webhooks.models import (
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)


class InMemoryWebhookRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.deliveries: dict[str, ReceivedDelivery] = {}
        self.code_change_requests: dict[str, str] = {}
        self.jobs: list[str] = []
        self.fail = fail
        self.calls = 0

    async def record(self, delivery: ReceivedDelivery) -> Outcome:
        self.calls += 1
        if self.fail:
            raise PersistenceError("boom")
        if delivery.delivery_id in self.deliveries:
            return Outcome.DUPLICATE
        self.deliveries[delivery.delivery_id] = delivery
        if delivery.status is ProcessingStatus.IGNORED:
            return Outcome.IGNORED
        assert delivery.issue is not None
        if delivery.issue.issue_id in self.code_change_requests:
            return Outcome.DUPLICATE
        self.code_change_requests[delivery.issue.issue_id] = delivery.delivery_id
        self.jobs.append(delivery.issue.issue_id)
        return Outcome.ACCEPTED
