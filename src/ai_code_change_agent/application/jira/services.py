"""Use case: read a Story (issue plus comments) through the port. Read-only."""

from ai_code_change_agent.application.jira.errors import JiraPermissionError
from ai_code_change_agent.application.jira.models import JiraStory
from ai_code_change_agent.application.jira.ports import JiraIssueReader


class FetchJiraStory:
    def __init__(self, reader: JiraIssueReader) -> None:
        self._reader = reader

    async def execute(self, issue_ref: str) -> JiraStory:
        issue = await self._reader.get_issue(issue_ref)
        try:
            comments = await self._reader.get_comments(issue_ref)
        except JiraPermissionError as exc:
            return JiraStory(issue=issue, comments=None, comments_unavailable_reason=exc.code)
        return JiraStory(issue=issue, comments=comments)
