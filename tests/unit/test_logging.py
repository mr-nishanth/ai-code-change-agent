import json

import pytest

from ai_code_change_agent.core.logging import (
    REDACTED,
    configure_logging,
    get_logger,
    redact_sensitive,
)


def test_redacts_sensitive_keys() -> None:
    out = redact_sensitive(
        None,
        "info",
        {"event": "x", "jira_webhook_secret": "s", "Authorization": "a", "issue_key": "SHOP-1"},
    )
    assert out["jira_webhook_secret"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["issue_key"] == "SHOP-1"


@pytest.mark.parametrize(
    "key",
    ["app_secret", "api_token", "PASSWORD", "passwd", "Authorization", "x_signature", "api_key",
     "api-key", "apikey", "aws_credentials"],
)  # fmt: skip
def test_every_sensitive_key_family_is_redacted(key: str) -> None:
    assert redact_sensitive(None, "info", {key: "v"})[key] == REDACTED


def test_emits_json_without_secret(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    get_logger("t").info("hello", api_token="abc123", issue_key="SHOP-1")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["level"] == "info"
    assert line["api_token"] == REDACTED
    assert "abc123" not in json.dumps(line)
