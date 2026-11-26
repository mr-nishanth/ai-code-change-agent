"""Scope policy. All values are configuration; nothing is hardcoded."""

from dataclasses import dataclass

from ai_code_change_agent.domain.webhooks.models import IgnoreReason, IssueRef


@dataclass(frozen=True)
class ScopePolicy:
    supported_event: str
    allowed_project_keys: frozenset[str]
    allowed_issue_types: frozenset[str]

    def check_event(self, event: str) -> IgnoreReason | None:
        return None if event == self.supported_event else IgnoreReason.EVENT_NOT_SUPPORTED

    def check_issue(self, issue: IssueRef) -> IgnoreReason | None:
        if issue.project_key not in self.allowed_project_keys:
            return IgnoreReason.PROJECT_OUT_OF_SCOPE
        if issue.issue_type not in self.allowed_issue_types:
            return IgnoreReason.ISSUE_TYPE_OUT_OF_SCOPE
        return None
