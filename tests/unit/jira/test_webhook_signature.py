import hmac
from collections.abc import Mapping
from typing import Any

import pytest

from ai_code_change_agent.domain.webhooks.models import RejectionCode
from ai_code_change_agent.integrations.jira.webhook import JiraCloudSignatureVerifier
from tests.support.jira import SECRET, sign

BODY = b'{"webhookEvent":"jira:issue_created"}'
V = JiraCloudSignatureVerifier(SECRET)


def verify(
    body: bytes, sig: str | None, *, v: JiraCloudSignatureVerifier = V
) -> RejectionCode | None:
    h: Mapping[str, str] = {} if sig is None else {"x-hub-signature": sig}
    return v.verify(body, h).failure


def test_valid_signature() -> None:
    result = V.verify(BODY, {"x-hub-signature": sign(BODY)})
    assert result.ok and result.failure is None


def test_uppercase_hex_and_method_accepted() -> None:
    sig = sign(BODY)
    method, digest = sig.split("=")
    assert verify(BODY, f"{method.upper()}={digest.upper()}") is None


@pytest.mark.parametrize("sig", [None, ""])
def test_missing_signature(sig: str | None) -> None:
    assert verify(BODY, sig) is RejectionCode.SIGNATURE_MISSING


@pytest.mark.parametrize(
    "sig", ["nosplit", "=abc", "sha256", "sha256=" + "a" * 63, "sha256=" + "g" * 64]
)
def test_malformed_signature(sig: str) -> None:
    assert verify(BODY, sig) is RejectionCode.SIGNATURE_MALFORMED


@pytest.mark.parametrize("method", ["sha1", "sha512", "md5"])
def test_unsupported_method_fails_closed(method: str) -> None:
    digest = sign(BODY).split("=")[1]
    assert verify(BODY, f"{method}={digest}") is RejectionCode.SIGNATURE_UNSUPPORTED_METHOD


def test_wrong_secret_mismatch() -> None:
    assert (
        verify(BODY, sign(BODY, secret="another-secret-0123456789"))
        is RejectionCode.SIGNATURE_MISMATCH
    )


def test_altered_body_mismatch() -> None:
    assert verify(BODY + b" ", sign(BODY)) is RejectionCode.SIGNATURE_MISMATCH


def test_whitespace_sensitive_raw_bytes() -> None:
    compact = b'{"a":1}'
    pretty = b'{ "a": 1 }'
    assert verify(pretty, sign(compact)) is RejectionCode.SIGNATURE_MISMATCH
    assert verify(pretty, sign(pretty)) is None


def test_unicode_payload_over_exact_utf8_bytes() -> None:
    body = '{"summary":"caf\u00e9 \u2713 \U0001f680"}'.encode()
    assert verify(body, sign(body)) is None


def test_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr("ai_code_change_agent.integrations.jira.webhook.hmac.compare_digest", spy)
    verify(BODY, sign(BODY))
    assert calls == [1]


def test_secret_not_in_repr() -> None:
    assert SECRET not in repr(V)


def test_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        JiraCloudSignatureVerifier("short")
