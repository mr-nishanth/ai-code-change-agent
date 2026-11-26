from datetime import UTC, datetime
from typing import Any

import pytest

from ai_code_change_agent.application.jira.errors import JiraResponseError
from ai_code_change_agent.integrations.jira.mapper import map_comment, map_issue, parse_timestamp
from tests.support.jira_rest import adf, comment_payload, issue_payload


def test_full_issue_mapping() -> None:
    issue = map_issue(issue_payload(), {})
    assert (issue.id, issue.key) == ("10001", "TST-1")
    assert issue.summary == "Synthetic story"
    assert issue.description is not None
    assert issue.description.text == "Do the thing"
    assert issue.issue_type == "Story"
    assert issue.project is not None
    assert (issue.project.id, issue.project.key, issue.project.name) == (
        "100",
        "TST",
        "Test Project",
    )
    assert issue.status == "To Do"
    assert issue.priority == "Medium"
    assert issue.labels == ("alpha", "beta")
    assert issue.components == ("core", "api")
    assert issue.assignee is not None
    assert issue.assignee.account_id == "acc-1"
    assert issue.assignee.display_name == "Ada Example"
    assert issue.assignee.active is True
    assert issue.reporter is not None
    assert issue.reporter.display_name == "Bob Example"
    assert issue.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert issue.updated_at is not None
    assert issue.updated_at.microsecond == 123000
    assert issue.warnings == ()


def test_user_email_is_never_mapped() -> None:
    issue = map_issue(issue_payload(), {})
    assert issue.assignee is not None
    assert "ada@corp.invalid" not in issue.model_dump_json()


def test_minimal_issue_has_nulls_not_errors() -> None:
    issue = map_issue({"id": "5", "key": "A-1", "fields": {}}, {})
    assert issue.summary is None
    assert issue.description is None
    assert issue.project is None
    assert issue.labels == ()
    assert issue.assignee is None
    assert issue.created_at is None


def test_missing_fields_object_is_tolerated() -> None:
    assert map_issue({"id": "5", "key": "A-1"}, {}).summary is None


@pytest.mark.parametrize("raw", [{}, {"id": "1"}, {"key": "A-1"}, {"id": 1, "key": "A-1"}, []])
def test_missing_identity_is_response_error(raw: Any) -> None:
    with pytest.raises(JiraResponseError):
        map_issue(raw, {})


@pytest.mark.parametrize(
    ("raw", "expected_year"),
    [
        ("2026-01-02T03:04:05.000+0000", 2026),
        ("2026-01-02T03:04:05.000Z", 2026),
        ("2026-01-02T03:04:05+00:00", 2026),
    ],
)
def test_parse_timestamp_variants(raw: str, expected_year: int) -> None:
    parsed = parse_timestamp(raw)
    assert parsed is not None
    assert parsed.year == expected_year
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("raw", [None, "", "not a date", 5, "2026-13-45T00:00:00.000+0000"])
def test_parse_timestamp_invalid_is_none(raw: Any) -> None:
    assert parse_timestamp(raw) is None


def test_malformed_timestamp_adds_warning() -> None:
    issue = map_issue(issue_payload(created="garbage"), {})
    assert issue.created_at is None
    assert "timestamp_invalid:created" in issue.warnings


def test_invalid_label_entries_are_reported() -> None:
    issue = map_issue(issue_payload(labels=["ok", 5, None]), {})
    assert issue.labels == ("ok",)
    assert "label_invalid" in issue.warnings


def test_custom_field_kinds() -> None:
    fields: dict[str, Any] = {
        "customfield_1": "free text",
        "customfield_2": 42,
        "customfield_3": {"value": "Opt A", "id": "1"},
        "customfield_4": {"value": "Parent", "child": {"value": "Child"}},
        "customfield_5": [{"value": "X"}, {"value": "Y"}],
        "customfield_6": {"accountId": "acc-9", "displayName": "Cy"},
        "customfield_7": adf("rich body"),
        "customfield_8": None,
        "customfield_9": True,
        "customfield_10": ["a", "b"],
    }
    mapping = {f"f{i}": f"customfield_{i}" for i in range(1, 11)}
    issue = map_issue(issue_payload(**fields), mapping)
    cf = issue.custom_fields
    assert (cf["f1"].kind, cf["f1"].text) == ("text", "free text")
    assert (cf["f2"].kind, cf["f2"].text) == ("number", "42")
    assert (cf["f3"].kind, cf["f3"].text) == ("option", "Opt A")
    assert cf["f4"].text == "Parent / Child"
    assert (cf["f5"].kind, cf["f5"].values) == ("options", ("X", "Y"))
    assert (cf["f6"].kind, cf["f6"].text) == ("user", "Cy")
    assert cf["f7"].kind == "rich_text"
    assert cf["f7"].rich_text is not None
    assert cf["f7"].rich_text.text == "rich body"
    assert cf["f8"].kind == "null"
    assert (cf["f9"].kind, cf["f9"].text) == ("boolean", "true")
    assert cf["f10"].values == ("a", "b")
    assert all(c.field_id.startswith("customfield_") for c in cf.values())


def test_unsupported_custom_field_is_flagged_not_dropped() -> None:
    issue = map_issue(
        issue_payload(customfield_1={"weird": {"nested": [1]}}), {"f1": "customfield_1"}
    )
    assert issue.custom_fields["f1"].kind == "unsupported"
    assert "custom_field_unsupported:f1" in issue.warnings


def test_unsupported_list_member_flags_whole_field() -> None:
    issue = map_issue(issue_payload(customfield_1=[{"weird": 1}]), {"f1": "customfield_1"})
    assert issue.custom_fields["f1"].kind == "unsupported"


def test_absent_custom_field_is_reported() -> None:
    issue = map_issue(issue_payload(), {"f1": "customfield_1"})
    assert "f1" not in issue.custom_fields
    assert "custom_field_absent:f1" in issue.warnings


def test_comment_mapping() -> None:
    comment = map_comment(comment_payload("7", "hi"))
    assert comment.id == "7"
    assert comment.body is not None
    assert comment.body.text == "hi"
    assert comment.author is not None
    assert comment.author.display_name == "Ada Example"
    assert comment.created_at is not None


@pytest.mark.parametrize("raw", [{}, {"id": 5}, [], None])
def test_comment_without_id_is_response_error(raw: Any) -> None:
    with pytest.raises(JiraResponseError):
        map_comment(raw)
