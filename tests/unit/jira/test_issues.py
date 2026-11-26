import json
from typing import Any

import httpx
import pytest

from ai_code_change_agent.application.jira.errors import (
    JiraInvalidIssueReferenceError,
    JiraIssueNotFoundError,
    JiraResponseError,
)
from ai_code_change_agent.integrations.jira.issues import JiraCloudIssueReader
from tests.support.jira_rest import (
    comment_payload,
    comments_page,
    issue_payload,
    json_response,
    make_client,
)


def _reader(
    handler: Any,
    *,
    mapping: dict[str, str] | None = None,
    page_size: int = 2,
    max_comments: int = 100,
) -> JiraCloudIssueReader:
    return JiraCloudIssueReader(
        make_client(handler),
        field_map=mapping or {},
        comment_page_size=page_size,
        max_comments=max_comments,
    )


async def test_get_issue_by_key_requests_only_needed_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(issue_payload(customfield_10042="Acme"))

    issue = await _reader(handler, mapping={"vendor": "customfield_10042"}).get_issue("TST-1")
    assert issue.custom_fields["vendor"].text == "Acme"
    request = seen[0]
    assert request.url.path == "/rest/api/3/issue/TST-1"
    fields = request.url.params["fields"].split(",")
    assert "customfield_10042" in fields
    assert {
        "summary",
        "description",
        "issuetype",
        "project",
        "status",
        "priority",
        "labels",
        "components",
        "assignee",
        "reporter",
        "created",
        "updated",
    } <= set(fields)
    assert "*all" not in fields
    assert [r.method for r in seen] == ["GET"]


async def test_get_issue_by_numeric_id() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return json_response(issue_payload())

    await _reader(handler).get_issue("10001")
    assert paths == ["/rest/api/3/issue/10001"]


@pytest.mark.parametrize(
    "ref",
    [
        "",
        " ",
        "abc",
        "tst-1",
        "TST-",
        "TST-1/../../x",
        "../x",
        "TST-1?x=1",
        "TST-1#f",
        "TST 1",
        "TST-1\n",
        "http://evil.invalid",
        "A-" + "9" * 12,
        "1" * 25,
        "-1",
    ],
)
async def test_invalid_refs_never_reach_the_network(ref: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be touched")

    reader = _reader(handler)
    with pytest.raises(JiraInvalidIssueReferenceError):
        await reader.get_issue(ref)
    with pytest.raises(JiraInvalidIssueReferenceError):
        await reader.get_comments(ref)


async def test_issue_not_found_propagates() -> None:
    with pytest.raises(JiraIssueNotFoundError):
        await _reader(lambda _: httpx.Response(404, text="nope")).get_issue("TST-9")


async def test_malformed_issue_payload_is_response_error() -> None:
    with pytest.raises(JiraResponseError):
        await _reader(lambda _: json_response({"unexpected": True})).get_issue("TST-1")


async def test_comments_paginate_with_configured_page_size() -> None:
    all_items = [comment_payload(str(i), f"c{i}") for i in range(5)]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        start = int(request.url.params["startAt"])
        size = int(request.url.params["maxResults"])
        return json_response(
            comments_page(all_items[start : start + size], start_at=start, total=5)
        )

    result = await _reader(handler, page_size=2).get_comments("TST-1")
    assert [c.id for c in result.items] == ["0", "1", "2", "3", "4"]
    assert (result.total, result.truncated) == (5, False)
    assert [(r.url.params["startAt"], r.url.params["maxResults"]) for r in seen] == [
        ("0", "2"),
        ("2", "2"),
        ("4", "2"),
    ]
    assert all(r.url.path == "/rest/api/3/issue/TST-1/comment" for r in seen)
    assert all(r.method == "GET" for r in seen)
    assert result.items[0].body is not None
    assert result.items[0].body.text == "c0"


async def test_comment_total_cap_marks_truncation() -> None:
    items = [comment_payload(str(i)) for i in range(10)]

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startAt"])
        size = int(request.url.params["maxResults"])
        return json_response(comments_page(items[start : start + size], start_at=start, total=10))

    result = await _reader(handler, page_size=4, max_comments=6).get_comments("TST-1")
    assert len(result.items) == 6
    assert result.truncated is True
    assert result.total == 10


async def test_no_comments() -> None:
    result = await _reader(lambda _: json_response(comments_page([]))).get_comments("TST-1")
    assert (result.items, result.total, result.truncated) == ((), 0, False)


async def test_malformed_comment_is_dropped_with_warning() -> None:
    page = comments_page([comment_payload("1"), {"no": "id"}, comment_payload("3")], total=3)
    result = await _reader(lambda _: json_response(page), page_size=10).get_comments("TST-1")
    assert [c.id for c in result.items] == ["1", "3"]
    assert result.warnings == ("comment_malformed:1",)


@pytest.mark.parametrize(
    "body",
    [
        {"comments": "x"},
        {"total": 1},
        {"comments": [], "total": "x"},
        {"comments": ["notadict"], "total": 1},
    ],
)
async def test_bad_comment_page_shape(body: dict[str, Any]) -> None:
    if body.get("comments") == ["notadict"]:
        result = await _reader(lambda _: json_response(body), page_size=10).get_comments("TST-1")
        assert result.items == ()
        assert result.warnings
        return
    with pytest.raises(JiraResponseError):
        await _reader(lambda _: json_response(body)).get_comments("TST-1")


async def test_response_json_is_never_exposed_raw() -> None:
    reader = _reader(lambda _: json_response(issue_payload()))
    issue = await reader.get_issue("TST-1")
    assert not isinstance(issue, httpx.Response | dict)
    json.loads(issue.model_dump_json())
