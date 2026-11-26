import pytest

from ai_code_change_agent.application.jira.errors import (
    JiraIssueNotFoundError,
    JiraPermissionError,
    JiraRateLimitError,
)
from ai_code_change_agent.application.jira.models import (
    JiraComment,
    JiraCommentList,
    JiraIssue,
)
from ai_code_change_agent.application.jira.ports import JiraIssueReader
from ai_code_change_agent.application.jira.services import FetchJiraStory


class FakeReader:
    def __init__(
        self, *, comments_error: Exception | None = None, issue_error: Exception | None = None
    ) -> None:
        self.comments_error = comments_error
        self.issue_error = issue_error
        self.calls: list[str] = []

    async def get_issue(self, issue_ref: str) -> JiraIssue:
        self.calls.append(f"issue:{issue_ref}")
        if self.issue_error:
            raise self.issue_error
        return JiraIssue(id="1", key="TST-1")

    async def get_comments(self, issue_ref: str) -> JiraCommentList:
        self.calls.append(f"comments:{issue_ref}")
        if self.comments_error:
            raise self.comments_error
        return JiraCommentList(items=(JiraComment(id="9"),), total=1, truncated=False)


def _port(reader: JiraIssueReader) -> JiraIssueReader:
    return reader


async def test_story_combines_issue_and_comments_via_port() -> None:
    reader = FakeReader()
    story = await FetchJiraStory(_port(reader)).execute("TST-1")
    assert story.issue.key == "TST-1"
    assert story.comments is not None
    assert story.comments.items[0].id == "9"
    assert story.comments_unavailable_reason is None
    assert reader.calls == ["issue:TST-1", "comments:TST-1"]


async def test_comment_permission_denied_degrades_explicitly() -> None:
    story = await FetchJiraStory(
        _port(FakeReader(comments_error=JiraPermissionError(operation="get_comments")))
    ).execute("TST-1")
    assert story.comments is None
    assert story.comments_unavailable_reason == "jira_permission_denied"


async def test_issue_errors_propagate() -> None:
    with pytest.raises(JiraIssueNotFoundError):
        await FetchJiraStory(
            _port(FakeReader(issue_error=JiraIssueNotFoundError(operation="get_issue")))
        ).execute("TST-1")


async def test_other_comment_errors_propagate() -> None:
    with pytest.raises(JiraRateLimitError):
        await FetchJiraStory(
            _port(FakeReader(comments_error=JiraRateLimitError(operation="get_comments")))
        ).execute("TST-1")
