"""Story extraction and the single Jira write (outcome comment)."""

import json

import httpx
from pydantic import SecretStr

from ai_code_change_agent.application.jira.models import JiraIssue, JiraRichText
from ai_code_change_agent.application.jira.story import acceptance_criteria, story_of
from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.reporting import JiraOutcomeReporter, ReportOutcome


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
