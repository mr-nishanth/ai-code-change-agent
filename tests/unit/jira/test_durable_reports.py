"""Durable Jira reporting (`DurableJiraReporter`): stored before the POST, retried on transient
outcomes within a round and across restarts, never posted twice, permanent errors not retried."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from structlog.testing import capture_logs

from ai_code_change_agent.application.jira import reports
from ai_code_change_agent.application.jira.reports import (
    DurableJiraReporter,
    ReportState,
    marker,
    report_text,
)
from tests.support.jira_reports import Clock, CommentJira, MemoryReportStore

KEY = "SHOP-1"


class Rig:
    def __init__(self, jira: CommentJira, *, enabled: bool = True) -> None:
        self.jira = jira
        self.clock = Clock()
        self.store = MemoryReportStore(self.clock)
        self.pauses: list[float] = []

        async def sleep(seconds: float) -> None:
            self.pauses.append(seconds)

        self.reporter = DurableJiraReporter(
            jira.reporter(enabled=enabled), self.store, now=self.clock, sleep=sleep,
            jitter=lambda: 0.5,
        )  # fmt: skip

    async def state(self, report_id: str = "attempt-1") -> Any:
        return await self.store.get(report_id)


async def test_a_200_posts_once_with_the_marker_and_settles_posted() -> None:
    rig = Rig(CommentJira())
    assert await rig.reporter.report(KEY, "Done.", report_id="attempt-1") == "POSTED"
    rec = await rig.state()
    assert rec.state is ReportState.POSTED and rec.attempts == 1 and rec.comment_id == "10001"
    assert rig.jira.texts() == [report_text("attempt-1", "Done.")] and rig.jira.gets == 0
    assert f"Report ID: {marker('attempt-1')}" in rig.jira.texts()[0]
    # the same report again (a replayed cleanup): stored once, never posted again
    assert await rig.reporter.report(KEY, "Done again.", report_id="attempt-1") == "POSTED"
    assert rig.jira.posts == 1 and await rig.store.due(10) == []


async def test_a_429_waits_retry_after_then_posts_after_checking_for_the_marker() -> None:
    rig = Rig(CommentJira(script=[(429, {"Retry-After": "3"})]))
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "POSTED"
    assert rig.pauses == [3.0] and rig.jira.posts == 2 and rig.jira.gets == 1
    assert len(rig.jira.comments) == 1


@pytest.mark.parametrize("failure", [500, 503])
async def test_a_transient_failure_is_retried_in_the_round(failure: Any) -> None:
    rig = Rig(CommentJira(script=[failure]))
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "POSTED"
    assert len(rig.jira.comments) == 1 and rig.jira.gets == 1  # looked first: none yet
    assert rig.pauses == [1.5]  # 1 s base with jitter


@pytest.mark.parametrize("failure", ["timeout", "accepted_timeout"])
async def test_an_unknown_outcome_ends_the_round_and_the_next_one_looks_first(failure: str) -> None:
    """A POST that timed out may still be created by Jira: no lookup or second POST in that
    round; the next round starts no sooner than the client timeout and looks for the marker."""
    rig = Rig(CommentJira(script=[failure]))
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "FAILED"
    rec = await rig.state()
    assert rig.jira.posts == 1 and rig.jira.gets == 0 and rig.pauses == []
    assert rec.state is ReportState.PENDING and rec.last_error == "timeout"
    assert rec.next_retry_at >= rig.clock() + timedelta(seconds=21)  # 20 s client timeout + 1
    rig.clock.advance(reports.RETRY_MAX_SECONDS)
    assert await rig.reporter.retry_due() == 1
    assert (await rig.state()).state is ReportState.POSTED
    assert len(rig.jira.comments) == 1  # found by its marker when Jira had created it
    assert rig.jira.posts == (1 if failure == "accepted_timeout" else 2)


async def test_a_comment_by_someone_else_carrying_the_marker_does_not_suppress_the_report() -> None:
    jira = CommentJira(script=["timeout"])
    rig = Rig(jira)
    await rig.reporter.report(KEY, "x", report_id="attempt-1")
    jira.comments.append((f"/rest/api/3/issue/{KEY}/comment", marker("attempt-1"), "user-9"))
    rig.clock.advance(reports.RETRY_MAX_SECONDS)
    await rig.reporter.retry_due()
    assert (await rig.state()).state is ReportState.POSTED and jira.posts == 2


async def test_the_maintenance_retry_posts_once_per_report_and_is_capped() -> None:
    rig = Rig(CommentJira(script=[503] * 100))
    for n in range(reports.DUE_BATCH + 3):
        await rig.store.create(f"attempt-{n}", KEY, "body", "sha")
    assert await rig.reporter.retry_due() == reports.DUE_BATCH  # capped per round
    assert rig.jira.posts == reports.DUE_BATCH and rig.pauses == []  # one try each, no pauses


async def test_prepare_stores_without_posting_and_the_retry_delivers_it() -> None:
    rig = Rig(CommentJira())
    assert await rig.reporter.prepare(KEY, "x", report_id="intake-1")
    assert not await rig.reporter.prepare(KEY, "other", report_id="intake-1")  # first text wins
    assert rig.jira.posts == 0 and await rig.reporter.retry_due() == 1
    assert rig.jira.texts() == [report_text("intake-1", "x")]


async def test_a_parent_report_keeps_its_legacy_marker_and_reconciles_first() -> None:
    jira = CommentJira()
    jira.comments.append((f"/rest/api/3/issue/{KEY}/comment", "old\nReport ID: aica-report-p1-r0"))
    rig = Rig(jira)
    assert marker("parent-p1-r0") == "aica-report-p1-r0"
    outcome = await rig.reporter.report(KEY, "x", report_id="parent-p1-r0", reconcile=True)
    assert outcome == "POSTED" and jira.posts == 0  # found the comment posted before the upgrade


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_a_permanent_failure_is_never_retried(status: int) -> None:
    rig = Rig(CommentJira(script=[status]))
    with capture_logs() as logs:
        assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "PERMANENT_FAILURE"
    rec = await rig.state()
    assert rec.state is ReportState.FAILED_PERMANENT and rec.last_error == f"http_{status}"
    assert rig.jira.posts == 1 and rig.pauses == [] and await rig.store.due(10) == []
    rig.clock.advance(10_000)
    assert await rig.reporter.retry_due() == 0 and rig.jira.posts == 1
    assert any(e["event"] == "jira_report_failed_permanent" for e in logs)
    assert "tok-synthetic" not in repr(logs)


async def test_a_round_that_stays_transient_is_retried_later_and_recovers() -> None:
    rig = Rig(CommentJira(script=[503, 503, 503]))
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "FAILED"
    rec = await rig.state()
    assert rec.state is ReportState.PENDING and rec.last_error == "http_503" and rec.attempts == 1
    assert rec.next_retry_at == rig.clock() + timedelta(seconds=30)  # 30 s x (0.75+0.25)
    assert await rig.reporter.retry_due() == 0  # not due yet
    rig.clock.advance(31)
    assert await rig.reporter.retry_due() == 1
    rec = await rig.state()
    assert rec.state is ReportState.POSTED and rec.attempts == 2 and len(rig.jira.comments) == 1


async def test_rounds_are_bounded_then_failed_permanent() -> None:
    rig = Rig(CommentJira(script=[503] * 100))
    await rig.reporter.report(KEY, "x", report_id="attempt-1")
    for _ in range(reports.MAX_ROUNDS):
        rig.clock.advance(reports.RETRY_MAX_SECONDS + 1)
        await rig.reporter.retry_due()
    rec = await rig.state()
    assert rec.state is ReportState.FAILED_PERMANENT and rec.attempts == reports.MAX_ROUNDS
    assert rec.last_error == "retries_exhausted:http_503" and rig.jira.comments == []
    assert await rig.store.due(10) == []


async def test_a_retry_after_too_long_for_the_round_schedules_the_next_one_after_it() -> None:
    rig = Rig(CommentJira(script=[(429, {"Retry-After": "120"})]))
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "FAILED"
    rec = await rig.state()
    assert rig.pauses == [] and rec.next_retry_at == rig.clock() + timedelta(seconds=120)


async def test_a_worker_crash_before_posting_is_recovered_once_the_claim_is_stale() -> None:
    rig = Rig(CommentJira())
    body = report_text("attempt-1", "x")
    assert await rig.store.create("attempt-1", KEY, body, "sha")
    assert await rig.store.claim("attempt-1", reports.STALE_SECONDS) is not None  # then crash
    assert await rig.reporter.retry_due() == 0  # in flight: nobody else posts it
    rig.clock.advance(reports.STALE_SECONDS + 1)
    assert await rig.reporter.retry_due() == 1
    assert (await rig.state()).state is ReportState.POSTED and rig.jira.texts() == [body]
    assert rig.jira.gets == 1  # the stale claim might have posted: looked first


async def test_a_worker_crash_after_jira_accepted_finds_the_comment_and_posts_nothing() -> None:
    jira = CommentJira()
    rig = Rig(jira)
    body = report_text("attempt-1", "x")
    await rig.store.create("attempt-1", KEY, body, "sha")
    await rig.store.claim("attempt-1", reports.STALE_SECONDS)
    jira.comments.append((f"/rest/api/3/issue/{KEY}/comment", body))  # POST went through, crash
    rig.clock.advance(reports.STALE_SECONDS + 1)
    await rig.reporter.retry_due()
    assert (await rig.state()).state is ReportState.POSTED and jira.posts == 0
    assert len(jira.comments) == 1


async def test_concurrent_deliveries_post_once() -> None:
    gate = asyncio.Event()

    class Slow(CommentJira):
        pass

    jira = Slow()
    rig = Rig(jira)
    inner = rig.reporter._reporter
    post = inner.post_comment

    async def slow_post(issue_key: str, text: str) -> Any:
        await gate.wait()
        return await post(issue_key, text)

    inner.post_comment = slow_post
    first = asyncio.create_task(rig.reporter.report(KEY, "x", report_id="attempt-1"))
    await asyncio.sleep(0)
    second = await rig.reporter.report(KEY, "x", report_id="attempt-1")  # claim refused
    gate.set()
    assert await first == "POSTED" and second == "FAILED" and jira.posts == 1


async def test_a_failed_lookup_never_posts_blindly() -> None:
    rig = Rig(CommentJira(script=[503], lookups=["timeout", "timeout"]))
    rig.reporter._reporter._account = "acc-reporting-bot"
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "FAILED"
    assert rig.jira.posts == 1 and rig.jira.gets == 2  # outcome unknown: no second POST
    assert (await rig.state()).last_error == "lookup_timeout"


async def test_disabled_reporting_stores_and_posts_nothing() -> None:
    rig = Rig(CommentJira(), enabled=False)
    assert await rig.reporter.report(KEY, "x", report_id="attempt-1") == "DISABLED"
    assert rig.store.items == {} and rig.jira.posts == 0
    assert await rig.reporter.retry_due() == 0


async def test_a_long_text_keeps_its_marker_and_a_report_without_id_posts_once() -> None:
    rig = Rig(CommentJira())
    await rig.reporter.report(KEY, "y" * 20_000, report_id="attempt-1")
    (text,) = rig.jira.texts()
    assert text.endswith(marker("attempt-1")) and len(text) < 8_000
    assert await rig.reporter.report(KEY, "z") == "POSTED"  # legacy: one plain POST
    assert rig.jira.posts == 2 and "DurableJiraReporter" in repr(rig.reporter)
    assert (await rig.reporter.find_comment(KEY, marker("attempt-1"))).status == "POSTED"
    assert (await rig.reporter.post_comment(KEY, "w")).status == "POSTED"
