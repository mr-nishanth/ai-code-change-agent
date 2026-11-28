"""Durable Jira reporting: every app-posted comment is a stored report that survives a crash.

One report per id (`attempt-<aid>` for a single-repository outcome, `intake-<rid>` for a Story
no attempt will start from), stored before any POST, so a failed or uncertain post is retried and
never repeated blindly. Lifecycle (`ReportState`):

    PENDING -> IN_PROGRESS -> POSTED | FAILED_PERMANENT | PENDING (next_retry_at, last_error)

* A claim (conditional write) moves PENDING (due) or a stale IN_PROGRESS (a crashed poster) to
  IN_PROGRESS and counts one delivery round; nobody else can post the report meanwhile.
* One round tries up to `TRIES_PER_ROUND` POSTs, pausing `Retry-After` (at most
  `MAX_PAUSE_SECONDS`) or a short exponential pause with jitter between them. Transient outcomes are
  429, 5xx, timeouts and transport errors; 400/401/403/404 and the like are permanent at once.
* The comment text ends with `Report ID: aica-report-<id>`. Jira has no idempotency key, so
  before any POST that might repeat an earlier one (a later try or round, or a stale claim) the
  issue's newest comments are searched for that marker; a match is recorded as POSTED.
* A round that ends transient schedules the next (`next_retry_at`, exponential with jitter, the
  `Retry-After` hint honoured) until `MAX_ROUNDS`, then FAILED_PERMANENT
  (`retries_exhausted:<code>`). The worker's dispatcher calls `retry_due` on its relay tick.

Reporting never changes an execution: callers keep the attempt/request status apart and only read
the outcome. Residual limitation: the marker search reads the newest 100 comments; a comment that
Jira created during an unknown outcome and that is followed by more than 100 newer comments before
the retry is not found and is posted again."""

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from ai_code_change_agent.core.logging import get_logger

_log = get_logger("jira_report")
MAX_ROUNDS = 6  # delivery rounds (claims) per report, across restarts; then FAILED_PERMANENT
TRIES_PER_ROUND = 3  # POSTs within one round (each later one after a marker lookup)
MAX_PAUSE_SECONDS = 10.0  # the longest in-round pause; a longer Retry-After ends the round
PAUSE_BASE_SECONDS = 1.0
RETRY_BASE_SECONDS = 30.0  # between rounds: 30 s, 60 s, ... capped at RETRY_MAX_SECONDS
RETRY_MAX_SECONDS = 900.0
STALE_SECONDS = 300  # an IN_PROGRESS claim older than this belongs to a crashed poster
TEXT_LIMIT = 7_900  # Jira comments are cut at 8000 characters; the marker line must survive
DUE_BATCH = 25


class ReportState(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    POSTED = "POSTED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


@dataclass(frozen=True)
class ReportRecord:
    report_id: str
    issue_key: str
    text: str
    content_sha256: str
    state: ReportState
    attempts: int
    next_retry_at: datetime | None = None
    last_error: str | None = None
    comment_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ReportClaim:
    record: ReportRecord  # as stored before the claim
    attempts: int  # delivery rounds including this one
    token: str  # only the holder of this claim may finish it


class JiraReportStore(Protocol):
    async def create(self, report_id: str, issue_key: str, text: str, sha256: str) -> bool:
        """Store a new PENDING report, due now. False when the id exists (one report per id)."""
        ...

    async def claim(self, report_id: str, stale_seconds: int) -> ReportClaim | None: ...

    async def finish(
        self,
        report_id: str,
        token: str,
        state: ReportState,
        *,
        next_retry_at: datetime | None = None,
        error: str | None = None,
        comment_id: str | None = None,
    ) -> bool:
        """Record the outcome of the claim `token`; False when that claim is no longer held."""
        ...

    async def due(self, limit: int) -> list[str]:
        """Ids of reports not POSTED/FAILED_PERMANENT whose next try is due (or claim stale)."""
        ...

    async def get(self, report_id: str) -> ReportRecord | None: ...

    async def recent(self, limit: int) -> list[ReportRecord]:
        """The newest reports, newest first (operator CLI)."""
        ...


def marker(report_id: str) -> str:
    return f"aica-report-{report_id}"


def report_text(report_id: str, text: str) -> str:
    """The posted text: the caller's (bounded) plus the marker line the retry searches for."""
    return f"{text[:TEXT_LIMIT].rstrip()}\nReport ID: {marker(report_id)}"


_OUTCOME = {
    ReportState.POSTED: "POSTED",
    ReportState.FAILED_PERMANENT: "PERMANENT_FAILURE",
}


class DurableJiraReporter:
    """The reporter every app-posted comment goes through (see the module docstring). `reporter`
    is the single-POST Jira client (`JiraOutcomeReporter`): `post_comment`, `find_comment`
    and `enabled`; its own `post_comment`/`find_comment` stay available for the
    multi-repository parent, which keeps its report state on the parent item."""

    def __init__(
        self,
        reporter: Any,
        store: JiraReportStore,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._reporter = reporter
        self._store = store
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._jitter = jitter

    def __repr__(self) -> str:
        return f"DurableJiraReporter({self._reporter!r})"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._reporter, "enabled", True))

    async def post_comment(self, issue_key: str, text: str) -> Any:
        return await self._reporter.post_comment(issue_key, text)

    async def find_comment(self, issue_key: str, marker_text: str) -> Any:
        return await self._reporter.find_comment(issue_key, marker_text)

    async def report(self, issue_key: str, text: str, *, report_id: str | None = None) -> str:
        """Store the report (once per id), then deliver it now. Returns "POSTED",
        "PERMANENT_FAILURE", "DISABLED", or "FAILED" while it is pending a retry (or another
        worker holds it)."""
        if not self.enabled:
            return "DISABLED"
        if report_id is None:  # nothing to key it on: one best-effort POST
            return str(await self._reporter.report(issue_key, text))
        body = report_text(report_id, text)
        sha = hashlib.sha256(body.encode()).hexdigest()
        if await self._store.create(report_id, issue_key, body, sha):
            _log.info("jira_report_pending", report_id=report_id, issue_key=issue_key)
        state = await self.deliver(report_id)
        return _OUTCOME.get(state, "FAILED") if state is not None else "FAILED"

    async def retry_due(self) -> int:
        """Deliver every due report (worker dispatcher tick); returns how many were tried."""
        if not self.enabled:
            return 0
        ids = await self._store.due(DUE_BATCH)
        for report_id in ids:
            await self.deliver(report_id)
        return len(ids)

    async def deliver(self, report_id: str) -> ReportState | None:
        """One delivery round when the report can be claimed; else its stored state."""
        claim = await self._store.claim(report_id, STALE_SECONDS)
        if claim is None:
            stored = await self._store.get(report_id)
            return stored.state if stored is not None else None
        record = claim.record
        log = _log.bind(report_id=report_id, issue_key=record.issue_key, round=claim.attempts)
        uncertain = claim.attempts > 1  # an earlier round may have reached Jira
        result: Any = None
        for tried in range(TRIES_PER_ROUND):
            result = None
            if uncertain:
                result = await self._reporter.find_comment(record.issue_key, marker(report_id))
                if result is not None:
                    log.info("jira_report_lookup", lookup=str(result.status))
            if result is None:  # confirmed absent, or a first try: post it
                result = await self._reporter.post_comment(record.issue_key, record.text)
            status = str(result.status)
            if status != "RETRYABLE":
                break
            uncertain = True  # a timeout or 5xx may hide a comment Jira created
            pause = self._pause(tried, getattr(result, "retry_after", None))
            if tried + 1 >= TRIES_PER_ROUND or pause is None:
                break
            log.info("jira_report_retry", error=result.error, pause_seconds=round(pause, 2))
            await self._sleep(pause)
        return await self._finish(claim, result, log)

    def _pause(self, tried: int, retry_after: float | None) -> float | None:
        if retry_after is not None and retry_after > 0:
            return retry_after if retry_after <= MAX_PAUSE_SECONDS else None
        return min(MAX_PAUSE_SECONDS, PAUSE_BASE_SECONDS * float(1 << tried) * (1 + self._jitter()))

    def _next_retry(self, rounds: int, retry_after: float | None) -> datetime:
        delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * float(1 << max(rounds - 1, 0)))
        delay *= 0.75 + 0.5 * self._jitter()  # +-25 %: concurrent failures spread out
        if retry_after is not None and retry_after > delay:
            delay = min(retry_after, RETRY_MAX_SECONDS)
        return self._now() + timedelta(seconds=delay)

    async def _finish(self, claim: ReportClaim, result: Any, log: Any) -> ReportState:
        report_id = claim.record.report_id
        status = str(result.status)
        error = (result.error or None) and str(result.error)[:120]
        retry_at = None
        if status == "POSTED":
            state, error = ReportState.POSTED, None
        elif status == "RETRYABLE" and claim.attempts < MAX_ROUNDS:
            state = ReportState.PENDING
            retry_at = self._next_retry(claim.attempts, getattr(result, "retry_after", None))
        else:
            state = ReportState.FAILED_PERMANENT
            if status == "RETRYABLE":
                error = f"retries_exhausted:{error}"[:120]
            elif status == "DISABLED":
                error = "reporting_disabled"
        recorded = await self._store.finish(
            report_id,
            claim.token,
            state,
            next_retry_at=retry_at,
            error=error,
            comment_id=result.comment_id if state is ReportState.POSTED else None,
        )
        fields = {"report_state": state.value, "error": error, "recorded": recorded,
                  "jira_status": getattr(result, "http_status", None)}  # fmt: skip
        if state is ReportState.FAILED_PERMANENT:
            log.warning("jira_report_failed_permanent", **fields)
        elif state is ReportState.PENDING:
            log.warning("jira_report_retry_scheduled", next_retry_at=str(retry_at), **fields)
        else:
            log.info("jira_report_posted", comment_id=result.comment_id, **fields)
        return state


__all__ = [
    "DurableJiraReporter",
    "JiraReportStore",
    "ReportClaim",
    "ReportRecord",
    "ReportState",
    "marker",
    "report_text",
]
