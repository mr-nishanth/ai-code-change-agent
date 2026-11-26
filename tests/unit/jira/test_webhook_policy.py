import pytest

from ai_code_change_agent.domain.webhooks.models import IgnoreReason, IssueRef
from ai_code_change_agent.domain.webhooks.policy import ScopePolicy

POLICY = ScopePolicy(
    supported_event="evt:created",
    allowed_project_keys=frozenset({"AAA", "BBB"}),
    allowed_issue_types=frozenset({"Story"}),
)


def issue(project: str = "AAA", itype: str = "Story") -> IssueRef:
    return IssueRef(issue_id="1", issue_key=f"{project}-1", project_key=project, issue_type=itype)


def test_supported_event_passes() -> None:
    assert POLICY.check_event("evt:created") is None


def test_other_event_is_ignored() -> None:
    assert POLICY.check_event("evt:updated") is IgnoreReason.EVENT_NOT_SUPPORTED


@pytest.mark.parametrize("project", ["AAA", "BBB"])
def test_allowed_projects_pass(project: str) -> None:
    assert POLICY.check_issue(issue(project)) is None


def test_project_out_of_scope_and_exact_case() -> None:
    assert POLICY.check_issue(issue("CCC")) is IgnoreReason.PROJECT_OUT_OF_SCOPE
    assert POLICY.check_issue(issue("aaa")) is IgnoreReason.PROJECT_OUT_OF_SCOPE


def test_issue_type_out_of_scope() -> None:
    assert POLICY.check_issue(issue(itype="Bug")) is IgnoreReason.ISSUE_TYPE_OUT_OF_SCOPE


def test_empty_allowlists_reject_everything() -> None:
    empty = ScopePolicy("e", frozenset(), frozenset())
    assert empty.check_issue(issue()) is IgnoreReason.PROJECT_OUT_OF_SCOPE
