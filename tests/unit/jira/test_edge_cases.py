from typing import Any

import httpx
import pytest

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.jira.adf import to_rich_text
from ai_code_change_agent.integrations.jira.client import _full_jitter
from ai_code_change_agent.integrations.jira.mapper import map_issue
from tests.support.jira_rest import issue_payload, make_client


def test_default_jitter_is_bounded() -> None:
    assert all(0.0 <= _full_jitter(2.0) <= 2.0 for _ in range(50))


@pytest.mark.parametrize(
    "path",
    ["/other/path", "/rest/api/3/../admin", "/rest/api/3//x", "https://evil.invalid/rest/api/3/x"],
)
async def test_client_refuses_non_api_paths(path: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be sent")

    with pytest.raises(ValueError, match="plain Jira REST API path"):
        await make_client(handler).get_json(path, operation="x")


def test_adf_attribute_fallbacks() -> None:
    doc: dict[str, Any] = {
        "type": "doc",
        "content": [
            "not-a-node",
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "attrs": {"text": 5}},
                    {"type": "date", "attrs": {"timestamp": "1700000000000"}},
                    {"type": "status"},
                ],
            },
            {"type": 7, "content": []},
            {"type": "paragraph", "content": [{"type": "text", "text": 5}]},
        ],
    }
    rich = to_rich_text(doc)
    assert rich is not None
    assert "@mention" in rich.text
    assert "1700000000000" in rich.text
    assert "unknown" in rich.unsupported_nodes


def test_custom_field_list_members() -> None:
    mapping = {"f": "customfield_1"}
    assert map_issue(issue_payload(customfield_1=[1, 2.5]), mapping).custom_fields["f"].values == (
        "1",
        "2.5",
    )
    assert (
        map_issue(issue_payload(customfield_1=[True]), mapping).custom_fields["f"].kind
        == "unsupported"
    )
    assert map_issue(issue_payload(customfield_1=[]), mapping).custom_fields["f"].values == ()
    assert (
        map_issue(issue_payload(customfield_1={"name": "N"}), mapping).custom_fields["f"].text
        == "N"
    )


def test_non_dict_user_and_project_are_none() -> None:
    issue = map_issue(issue_payload(assignee="x", project=5), {})
    assert issue.assignee is None
    assert issue.project is None


def test_repository_field_is_optional_and_empty_means_unset() -> None:
    assert Settings.model_validate({"app_env": "test"}).jira_repository_field is None
    assert (
        Settings.model_validate(
            {"app_env": "test", "jira_repository_field": "  "}
        ).jira_repository_field
        is None
    )
    s = Settings.model_validate({"app_env": "test", "jira_repository_field": "customfield_10050"})
    assert s.jira_repository_field == "customfield_10050"
