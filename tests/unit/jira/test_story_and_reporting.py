"""Story extraction and the single Jira write (outcome comment)."""

import json

import httpx
import pytest
from pydantic import SecretStr

from ai_code_change_agent.application.jira.models import JiraIssue, JiraRichText
from ai_code_change_agent.application.jira.story import acceptance_criteria, story_of
from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.reporting import (
    CommentStatus,
    JiraOutcomeReporter,
    ReportOutcome,
)


def test_acceptance_criteria_are_read_from_the_marked_section() -> None:
    text = (
        "Intro\n\nh3. Acceptance Criteria\n* greet returns Goodbye\n2. tests pass\n\n"
        "Notes after\n- not a criterion"
    )
    assert acceptance_criteria(text) == ("greet returns Goodbye", "tests pass")
    assert acceptance_criteria("no section here\n- item") == ()
    assert acceptance_criteria(None) == ()


def test_story_of_bounds_untrusted_text() -> None:
    issue = JiraIssue(
        id="1", key="SHOP-1", summary="s" * 900, description=JiraRichText(text="d" * 50_000)
    )
    story = story_of(issue)
    assert len(story.summary) == 500 and story.description is not None
    assert len(story.description) == 20_000


def reporter(handler: object, enabled: bool = True) -> JiraOutcomeReporter:
    return JiraOutcomeReporter(
        enabled=enabled,
        base_url="https://x.atlassian.net",
        authenticator=BasicAuthenticator("a@b.c", SecretStr("tok")),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_disabled_reporting_makes_no_request() -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(201)

    r = reporter(handle, enabled=False)
    assert await r.report("SHOP-1", "done") is ReportOutcome.DISABLED and calls == []
    assert "tok" not in repr(r)


async def test_one_comment_is_posted_and_nothing_else() -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(201, json={"id": "1"})

    assert await reporter(handle).report("SHOP-1", "line one\nline two") is ReportOutcome.POSTED
    (req,) = calls
    assert req.method == "POST" and req.url.path == "/rest/api/3/issue/SHOP-1/comment"
    body = json.loads(req.content)
    assert body["body"]["type"] == "doc" and len(body["body"]["content"]) == 2
    assert req.headers["authorization"].startswith("Basic ")


async def test_failures_are_reported_not_raised() -> None:
    assert (
        await reporter(lambda req: httpx.Response(403)).report("SHOP-1", "x")
        is ReportOutcome.FAILED
    )

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    assert await reporter(boom).report("SHOP-1", "x") is ReportOutcome.FAILED
    assert (
        await reporter(lambda req: httpx.Response(201)).report("bad key", "x")
        is ReportOutcome.FAILED
    )


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (401, CommentStatus.PERMANENT),
        (403, CommentStatus.PERMANENT),
        (404, CommentStatus.PERMANENT),
        (429, CommentStatus.RETRYABLE),
        (500, CommentStatus.RETRYABLE),
        (503, CommentStatus.RETRYABLE),
    ],
)
async def test_comment_post_failures_are_classified(code: int, status: CommentStatus) -> None:
    result = await reporter(lambda _: httpx.Response(code)).post_comment("SHOP-1", "x")
    assert result.status is status and result.http_status == code and result.comment_id is None


async def test_comment_post_returns_the_created_comment_id() -> None:
    result = await reporter(lambda _: httpx.Response(201, json={"id": "10042"})).post_comment(
        "SHOP-1", "x"
    )
    assert result.status is CommentStatus.POSTED and result.comment_id == "10042"


async def test_comment_post_timeout_is_retryable_and_never_retried_inside() -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ReadTimeout("slow", request=req)

    result = await reporter(handle).post_comment("SHOP-1", "x")
    assert result.status is CommentStatus.RETRYABLE and result.error == "timeout"
    assert len(calls) == 1  # an uncertain POST is reconciled by the caller, not repeated here


async def test_invalid_issue_key_is_permanent_without_a_request() -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(201)

    rep = reporter(handle)
    for key in ("", "kan-9", "KAN 9", "../x"):
        assert (await rep.post_comment(key, "x")).status is CommentStatus.PERMANENT
        assert (await rep.find_comment(key, "m")).status is CommentStatus.PERMANENT  # type: ignore[union-attr]
    assert calls == []


async def test_find_comment_matches_the_report_id_in_adf() -> None:
    body = {
        "comments": [
            {"id": "10008", "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Report ID: other"}]}]}},
            {"id": "10009", "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Report ID: aica-report-p-r1"}]}]}},
        ]
    }  # fmt: skip
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=body)

    rep = reporter(handle)
    found = await rep.find_comment("SHOP-1", "aica-report-p-r1")
    assert found is not None and found.comment_id == "10009"
    assert await rep.find_comment("SHOP-1", "aica-report-p-r2") is None
    assert seen[0].method == "GET" and seen[0].url.path == "/rest/api/3/issue/SHOP-1/comment"
    failed = await reporter(lambda _: httpx.Response(503)).find_comment("SHOP-1", "m")
    assert failed is not None and failed.status is CommentStatus.RETRYABLE
