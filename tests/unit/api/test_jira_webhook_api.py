import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.application.webhooks.receive import ReceiveJiraWebhook
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.domain.webhooks.policy import ScopePolicy
from ai_code_change_agent.integrations.jira.webhook import JiraCloudSignatureVerifier
from ai_code_change_agent.main import create_app
from tests.support.jira import EVENT, ISSUE_TYPE, PROJECT, SECRET, body_bytes, headers, sign
from tests.support.memory_repo import InMemoryWebhookRepository

URL = "/webhooks/jira"
LIMIT = 2048


def settings() -> Settings:
    return Settings.model_validate(
        {
            "app_env": "test",
            "jira_webhook_secret": SECRET,
            "jira_project_keys": PROJECT,
            "jira_story_issue_types": ISSUE_TYPE,
            "jira_webhook_event": EVENT,
            "jira_webhook_max_body_bytes": LIMIT,
        }
    )


@pytest.fixture
def repo() -> InMemoryWebhookRepository:
    return InMemoryWebhookRepository()


@pytest.fixture
def client(repo: InMemoryWebhookRepository) -> Iterator[TestClient]:
    app = create_app(settings())
    with TestClient(app) as c:
        app.state.webhook_service = ReceiveJiraWebhook(
            JiraCloudSignatureVerifier(SECRET),
            repo,
            ScopePolicy(EVENT, frozenset({PROJECT}), frozenset({ISSUE_TYPE})),
        )
        yield c


def post(c: TestClient, body: bytes, **kw: object) -> object:
    return c.post(URL, content=body, headers=headers(body, **kw))  # type: ignore[arg-type]


def test_valid_webhook_accepted(client: TestClient, repo: InMemoryWebhookRepository) -> None:
    body = body_bytes()
    r = post(client, body)
    assert r.status_code == 200  # type: ignore[attr-defined]
    assert r.json() == {"status": "accepted", "deliveryId": "delivery-0001"}  # type: ignore[attr-defined]
    assert len(repo.jobs) == 1


def test_duplicate_and_ignored_shapes(client: TestClient) -> None:
    body = body_bytes()
    post(client, body)
    assert post(client, body).json()["status"] == "duplicate"  # type: ignore[attr-defined]
    other = body_bytes(project="ZZZ")
    r = post(client, other, delivery_id="d-2")
    assert r.json() == {"status": "ignored", "deliveryId": "d-2", "reason": "project_out_of_scope"}  # type: ignore[attr-defined]


@pytest.mark.parametrize("sig", [None, "sha256=" + "0" * 64, "sha1=abc", "garbage"])
def test_bad_signature_is_401_with_generic_body(client: TestClient, sig: str | None) -> None:
    body = body_bytes()
    r = post(client, body, signature=sig)
    assert r.status_code == 401  # type: ignore[attr-defined]
    err = r.json()["error"]  # type: ignore[attr-defined]
    assert err["code"] == "unauthorized"
    assert err["request_id"] == r.headers["X-Request-ID"]  # type: ignore[attr-defined]


def test_malformed_requests_are_400(client: TestClient) -> None:
    body = body_bytes()
    assert post(client, body, delivery_id=None).status_code == 400  # type: ignore[attr-defined]
    bad = b"{oops"
    r = client.post(URL, content=bad, headers=headers(bad))
    assert r.status_code == 400 and r.json()["error"]["code"] == "payload_invalid"
    assert client.post(URL, content=b"", headers=headers(b"")).status_code == 400


def test_wrong_content_type_is_415(client: TestClient) -> None:
    assert post(client, body_bytes(), content_type="text/plain").status_code == 415  # type: ignore[attr-defined]


def test_oversize_by_content_length_is_413_even_when_signed(client: TestClient) -> None:
    body = body_bytes(summary="x" * (LIMIT + 10))
    assert post(client, body).status_code == 413  # type: ignore[attr-defined]


def test_oversize_streamed_without_content_length_is_413(client: TestClient) -> None:
    body = body_bytes(summary="x" * (LIMIT + 10))

    def chunks() -> Iterator[bytes]:
        for i in range(0, len(body), 512):
            yield body[i : i + 512]

    h = headers(body)
    r = client.post(URL, content=chunks(), headers=h)
    assert r.status_code == 413


def test_body_exactly_at_limit_is_accepted(client: TestClient) -> None:
    base = len(body_bytes(summary=""))
    body = body_bytes(summary="y" * (LIMIT - base))
    assert len(body) == LIMIT
    assert post(client, body).status_code == 200  # type: ignore[attr-defined]


def test_database_failure_is_500_without_leak(repo: InMemoryWebhookRepository) -> None:
    repo.fail = True
    app = create_app(settings())
    with TestClient(app) as c:
        app.state.webhook_service = ReceiveJiraWebhook(
            JiraCloudSignatureVerifier(SECRET),
            repo,
            ScopePolicy(EVENT, frozenset({PROJECT}), frozenset({ISSUE_TYPE})),
        )
        r = post(c, body_bytes())
    assert r.status_code == 500  # type: ignore[attr-defined]
    assert r.json()["error"]["code"] == "internal_error"  # type: ignore[attr-defined]
    assert "boom" not in r.text  # type: ignore[attr-defined]


def test_unconfigured_webhook_is_503() -> None:
    with TestClient(create_app(Settings.model_validate({"app_env": "test"}))) as c:
        r = post(c, body_bytes())
    assert r.status_code == 503  # type: ignore[attr-defined]


def test_get_not_allowed(client: TestClient) -> None:
    assert client.get(URL).status_code == 405


def test_logs_never_contain_secret_signature_or_payload(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    body = body_bytes(summary="TOP-SECRET-STORY-TEXT")
    post(client, body)
    post(client, body, signature="sha256=" + "ab" * 32)
    post(client, body, delivery_id="d-9")
    out = capsys.readouterr().out
    assert out
    for forbidden in (SECRET, sign(body), "ab" * 32, "TOP-SECRET-STORY-TEXT", "x-hub-signature"):
        assert forbidden not in out
    events = [json.loads(line)["event"] for line in out.splitlines() if line.startswith("{")]
    assert "webhook_processed" in events and "webhook_rejected" in events
