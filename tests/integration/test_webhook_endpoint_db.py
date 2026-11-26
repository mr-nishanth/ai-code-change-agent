from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app
from tests.integration.conftest import query
from tests.support.jira import EVENT, ISSUE_TYPE, PROJECT, SECRET, body_bytes, headers

pytestmark = pytest.mark.integration
URL = "/webhooks/jira"


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    cfg = Settings.model_validate(
        {
            "app_env": "test",
            "database_url": database_url,
            "jira_webhook_secret": SECRET,
            "jira_project_keys": PROJECT,
            "jira_story_issue_types": ISSUE_TYPE,
            "jira_webhook_event": EVENT,
        }
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_end_to_end_with_real_postgres(client: TestClient, database_url: str) -> None:
    body = body_bytes()
    r = client.post(URL, content=body, headers=headers(body))
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert client.post(URL, content=body, headers=headers(body)).json()["status"] == "duplicate"
    wrong = body_bytes(project="OTHER", issue_id="2", key="OTHER-2")
    assert (
        client.post(URL, content=wrong, headers=headers(wrong, delivery_id="d-2")).json()["status"]
        == "ignored"
    )
    bad = client.post(
        URL, content=body, headers=headers(body, delivery_id="d-3", signature="sha256=" + "0" * 64)
    )
    assert bad.status_code == 401
    assert query(database_url, "SELECT count(*) FROM code_change_requests") == [(1,)]
    assert query(database_url, "SELECT count(*) FROM jobs") == [(1,)]
    assert query(database_url, "SELECT count(*) FROM jira_deliveries") == [
        (2,)
    ]  # rejected delivery not stored


def test_readiness_is_200_with_real_database(client: TestClient) -> None:
    assert client.get("/health/ready").status_code == 200
