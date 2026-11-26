import hashlib
from typing import Any

import pytest

from ai_code_change_agent.application.webhooks.receive import ReceiveJiraWebhook
from ai_code_change_agent.domain.webhooks.models import (
    IgnoreReason,
    Outcome,
    PersistenceError,
    RejectionCode,
    WebhookRejected,
)
from ai_code_change_agent.domain.webhooks.policy import ScopePolicy
from ai_code_change_agent.integrations.jira.webhook import JiraCloudSignatureVerifier
from tests.support.jira import EVENT, ISSUE_TYPE, PROJECT, SECRET, body_bytes, headers, payload
from tests.support.memory_repo import InMemoryWebhookRepository

POLICY = ScopePolicy(EVENT, frozenset({PROJECT}), frozenset({ISSUE_TYPE}))


def make(
    repo: InMemoryWebhookRepository | None = None,
) -> tuple[ReceiveJiraWebhook, InMemoryWebhookRepository]:
    r = repo or InMemoryWebhookRepository()
    return ReceiveJiraWebhook(JiraCloudSignatureVerifier(SECRET), r, POLICY), r


async def reject_code(svc: ReceiveJiraWebhook, body: bytes, hdrs: dict[str, str]) -> RejectionCode:
    with pytest.raises(WebhookRejected) as exc:
        await svc.handle(body, hdrs)
    return exc.value.code


async def test_valid_new_delivery_is_accepted_and_recorded() -> None:
    svc, repo = make()
    body = body_bytes()
    res = await svc.handle(body, headers(body))
    assert res.outcome is Outcome.ACCEPTED and res.delivery_id == "delivery-0001"
    rec = repo.deliveries["delivery-0001"]
    assert rec.payload_sha256 == hashlib.sha256(body).hexdigest()
    assert rec.issue is not None and rec.issue.issue_key == "TST-1"
    assert repo.jobs == ["10001"]


async def test_duplicate_delivery_is_reported_duplicate() -> None:
    svc, repo = make()
    body = body_bytes()
    await svc.handle(body, headers(body))
    res = await svc.handle(body, headers(body))
    assert res.outcome is Outcome.DUPLICATE and len(repo.jobs) == 1


async def test_unsupported_event_ignored_without_issue() -> None:
    svc, repo = make()
    body = b'{"webhookEvent":"jira:issue_updated"}'
    res = await svc.handle(body, headers(body))
    assert res.outcome is Outcome.IGNORED and res.reason == IgnoreReason.EVENT_NOT_SUPPORTED
    assert repo.deliveries["delivery-0001"].issue is None and repo.jobs == []


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"project": "OTHER"}, IgnoreReason.PROJECT_OUT_OF_SCOPE),
        ({"issue_type": "Bug"}, IgnoreReason.ISSUE_TYPE_OUT_OF_SCOPE),
    ],
)
async def test_out_of_scope_is_ignored(kw: dict[str, Any], reason: IgnoreReason) -> None:
    svc, repo = make()
    body = body_bytes(**kw)
    res = await svc.handle(body, headers(body))
    assert res.outcome is Outcome.IGNORED and res.reason == reason and repo.jobs == []


async def test_invalid_signature_rejected_before_parse_and_persist() -> None:
    svc, repo = make()
    body = b"{not json"
    code = await reject_code(svc, body, headers(body, signature="sha256=" + "0" * 64))
    assert code is RejectionCode.SIGNATURE_MISMATCH and repo.calls == 0


async def test_missing_signature_rejected() -> None:
    svc, repo = make()
    body = body_bytes()
    assert (
        await reject_code(svc, body, headers(body, signature=None))
        is RejectionCode.SIGNATURE_MISSING
    )
    assert repo.calls == 0


async def test_modified_body_rejected() -> None:
    svc, _ = make()
    body = body_bytes()
    assert await reject_code(svc, body + b"\n", headers(body)) is RejectionCode.SIGNATURE_MISMATCH


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/xml"])
async def test_content_type_must_be_json(ctype: str | None) -> None:
    svc, _ = make()
    body = body_bytes()
    assert (
        await reject_code(svc, body, headers(body, content_type=ctype))
        is RejectionCode.CONTENT_TYPE_UNSUPPORTED
    )


async def test_json_content_type_with_charset_ok() -> None:
    svc, _ = make()
    body = body_bytes()
    res = await svc.handle(body, headers(body, content_type="Application/JSON; charset=utf-8"))
    assert res.outcome is Outcome.ACCEPTED


async def test_signed_empty_body_is_body_missing() -> None:
    svc, _ = make()
    assert await reject_code(svc, b"", headers(b"")) is RejectionCode.BODY_MISSING


@pytest.mark.parametrize("delivery_id", [None, "", "has space", "tab\tid", "x" * 256, "caf\u00e9"])
async def test_delivery_id_missing_or_malformed_rejected(delivery_id: str | None) -> None:
    svc, repo = make()
    body = body_bytes()
    assert (
        await reject_code(svc, body, headers(body, delivery_id=delivery_id))
        is RejectionCode.DELIVERY_ID_INVALID
    )
    assert repo.calls == 0


async def test_max_length_delivery_id_ok() -> None:
    svc, _ = make()
    body = body_bytes()
    assert (
        await svc.handle(body, headers(body, delivery_id="x" * 255))
    ).outcome is Outcome.ACCEPTED


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"[]",
        b'{"noevent": 1}',
        b'{"webhookEvent": ""}',
        b'{"webhookEvent": 5}',
        b'{"webhookEvent": "jira:issue_created"}',
        b'{"webhookEvent": "jira:issue_created", "issue": {"id": "abc", "key": "TST-1", "fields": {}}}',
    ],
)
async def test_malformed_payload_rejected(body: bytes) -> None:
    svc, repo = make()
    assert await reject_code(svc, body, headers(body)) is RejectionCode.PAYLOAD_INVALID
    assert repo.calls == 0


@pytest.mark.parametrize("key", ["lower-1", "TST-", "TST-12345678901", "TST 1", "1ABC-1"])
async def test_invalid_issue_key_rejected(key: str) -> None:
    svc, _ = make()
    body = body_bytes(payload(key=key))
    assert await reject_code(svc, body, headers(body)) is RejectionCode.PAYLOAD_INVALID


async def test_retry_and_flow_headers_parsed_defensively() -> None:
    svc, repo = make()
    body = body_bytes()
    await svc.handle(
        body,
        headers(
            body, extra={"x-atlassian-webhook-retry": "2", "x-atlassian-webhook-flow": "Primary"}
        ),
    )
    rec = repo.deliveries["delivery-0001"]
    assert (rec.retry_count, rec.flow) == (2, "Primary")
    svc2, repo2 = make()
    await svc2.handle(
        body,
        headers(
            body, extra={"x-atlassian-webhook-retry": "-5", "x-atlassian-webhook-flow": "evil"}
        ),
    )
    rec2 = repo2.deliveries["delivery-0001"]
    assert (rec2.retry_count, rec2.flow) == (0, None)


async def test_header_names_are_case_insensitive() -> None:
    svc, _ = make()
    body = body_bytes()
    h = {k.upper(): v for k, v in headers(body).items()}
    assert (await svc.handle(body, h)).outcome is Outcome.ACCEPTED


async def test_persistence_error_propagates() -> None:
    svc, _ = make(InMemoryWebhookRepository(fail=True))
    body = body_bytes()
    with pytest.raises(PersistenceError):
        await svc.handle(body, headers(body))
