"""Focused, read-only port. Implementations live in `integrations/jira`."""

from typing import Protocol

from ai_code_change_agent.application.jira.models import JiraCommentList, JiraIssue


class JiraIssueReader(Protocol):
    async def get_issue(self, issue_ref: str) -> JiraIssue: ...

    async def get_comments(self, issue_ref: str) -> JiraCommentList: ...
