"""Durable Jira reports on DynamoDB (Floci): the store's conditions and the retry index, driven by
the real `DurableJiraReporter` over a scripted Jira (mock transport). Own `s2116-<uuid>` table."""

import asyncio

import pytest

from ai_code_change_agent.application.jira import reports
from ai_code_change_agent.application.jira.reports import DurableJiraReporter, ReportState
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.jira_report_store import DynamoJiraReportStore
from tests.support.jira_reports import Clock, CommentJira

pytestmark = [pytest.mark.floci, pytest.mark.integration]
KEY = "SHOP-1"


def rig(
    table: Table, jira: CommentJira
) -> tuple[DurableJiraReporter, DynamoJiraReportStore, Clock]:
    clock = Clock()
    store = DynamoJiraReportStore(table, clock=clock)

    async def no_sleep(_: float) -> None:
        return None

    return DurableJiraReporter(jira.reporter(), store, now=clock, sleep=no_sleep), store, clock


async def test_a_report_is_stored_before_the_post_and_settles_out_of_the_retry_index(
    app_table: Table,
) -> None:
    jira = CommentJira()
    reporter, store, _ = rig(app_table, jira)
    assert await reporter.report(KEY, "Done.", report_id="attempt-a") == "POSTED"
    item = await app_table.get(keys.jira_report("attempt-a"))
    assert item is not None and item["state"] == "POSTED" and item["attempts"] == 1
    assert "GSI3PK" not in item and int(item["expires_at"]) > 0 and item["comment_id"] == "10001"
    assert item["GSI2PK"] == keys.GSI_LIST_REPORT and len(item["content_sha256"]) == 64
    assert "text" not in item  # claims and finishes rewrite only the small state item
    body = await app_table.get(keys.jira_report_text("attempt-a"))
    assert body is not None and body["text"].startswith("Done.")
    assert await store.text("attempt-a") == jira.texts()[0]
    assert not await store.create("attempt-a", KEY, "other", "sha")  # one report per id
    assert [r.report_id for r in await store.recent(10)] == ["attempt-a"]


async def test_transient_rounds_are_retried_from_the_index_and_bounded(app_table: Table) -> None:
    jira = CommentJira(script=[503] * 3)
    reporter, store, clock = rig(app_table, jira)
    assert await reporter.report(KEY, "x", report_id="attempt-b") == "FAILED"
    item = await app_table.get(keys.jira_report("attempt-b"))
    assert item is not None and (item["state"], item["last_error"]) == ("PENDING", "http_503")
    assert item["GSI3PK"] == keys.GSI_REPORT and await store.due(10) == []
    clock.advance(reports.RETRY_MAX_SECONDS)
    assert await store.due(10) == ["attempt-b"]
    assert await reporter.retry_due() == 1
    rec = await store.get("attempt-b")
    assert rec is not None and rec.state is ReportState.POSTED and len(jira.comments) == 1


async def test_a_stale_claim_is_recovered_once_and_concurrent_claims_get_one_winner(
    app_table: Table,
) -> None:
    jira = CommentJira()
    reporter, store, clock = rig(app_table, jira)
    assert await store.create("attempt-c", KEY, reports.report_text("attempt-c", "x"), "sha")
    claims = await asyncio.gather(*(store.claim("attempt-c", 300) for _ in range(6)))
    (held,) = [c for c in claims if c is not None]
    assert await store.due(10) == []  # in flight
    clock.advance(301)
    assert await store.due(10) == ["attempt-c"]  # the claimer crashed: due again
    jira.comments.append(
        (f"/rest/api/3/issue/{KEY}/comment", reports.report_text("attempt-c", "x"))
    )
    assert await reporter.retry_due() == 1
    assert jira.posts == 0 and (await store.get("attempt-c")).state is ReportState.POSTED  # type: ignore[union-attr]
    assert not await store.finish("attempt-c", held.token, ReportState.PENDING)  # stale holder


async def test_a_permanent_failure_is_recorded_once(app_table: Table) -> None:
    jira = CommentJira(script=[401])
    reporter, store, clock = rig(app_table, jira)
    assert await reporter.report(KEY, "x", report_id="intake-d") == "PERMANENT_FAILURE"
    clock.advance(100_000)
    assert await store.due(10) == [] and await reporter.retry_due() == 0 and jira.posts == 1
    rec = await store.get("intake-d")
    assert rec is not None and (rec.state, rec.last_error) == (
        ReportState.FAILED_PERMANENT,
        "http_401",
    )
