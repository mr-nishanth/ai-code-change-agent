"""Prints the task cost report when an attempt stops, and the session summary at shutdown."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ai_code_change_agent.application.costs.pricing import PriceTable
from ai_code_change_agent.application.costs.report import CostQuery, session_summary, task_report

log = logging.getLogger(__name__)
Launch = Callable[[uuid.UUID], Awaitable[Any]]
StatusOf = Callable[[uuid.UUID], Awaitable[tuple[str, bool]]]  # (status, terminal)


class CostReporter:
    def __init__(
        self,
        query: CostQuery,
        prices: PriceTable,
        *,
        provider: str,
        primary_model: str,
        session_id: uuid.UUID,
        status_of: StatusOf,
        write: Callable[[str], Any],
    ) -> None:
        self._query = query
        self._prices = prices
        self._provider = provider
        self._model = primary_model
        self.session_id = session_id
        self._status_of = status_of
        self._write = write

    def around(self, launch: Launch) -> Launch:
        """Run the workflow; print the attempt's cost when it ends (terminal or crashed)."""

        async def run(attempt_id: uuid.UUID) -> Any:
            crashed = True
            try:
                result = await launch(attempt_id)
                crashed = False
                return result
            finally:
                await self.attempt_report(attempt_id, crashed=crashed)

        return run

    async def attempt_report(self, attempt_id: uuid.UUID, *, crashed: bool = False) -> None:
        try:
            status, terminal = await self._status_of(attempt_id)
            if not (terminal or crashed):
                return  # paused at a manual gate: reported when it finishes
            head = await self._query.rows(attempt_ids=[attempt_id])
            issue = next((r.issue_key for r in head if r.issue_key), None)
            rows = await self._query.rows(issue_keys=[issue]) if issue else head
            result = status if terminal else f"{status} (not finished)"
            self._write(
                task_report(
                    attempt_id,
                    rows,
                    provider=self._provider,
                    primary_model=self._model,
                    pricing_version=self._prices.version,
                    result=result,
                )
                + "\n"
            )
        except Exception:  # a report must never fail the job
            log.warning("cost_report_failed", exc_info=True)

    async def print_session_summary(self) -> None:
        try:
            mine = await self._query.rows(session_id=self.session_id)
            issues = sorted({r.issue_key for r in mine if r.issue_key})
            tasks = await self._query.rows(issue_keys=issues) if issues else []
            self._write(
                session_summary(self.session_id, mine, tasks, pricing_version=self._prices.version)
                + "\n"
            )
        except Exception:
            log.warning("session_cost_summary_failed", exc_info=True)
