import base64

from pydantic import SecretStr

from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator

TOKEN = "tok-0123456789abcdef-synthetic"


def test_basic_header() -> None:
    auth = BasicAuthenticator("svc@corp.invalid", SecretStr(TOKEN))
    header = auth.headers()["Authorization"]
    assert header.startswith("Basic ")
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == f"svc@corp.invalid:{TOKEN}"


def test_repr_and_str_never_expose_credentials() -> None:
    auth = BasicAuthenticator("svc@corp.invalid", SecretStr(TOKEN))
    for text in (repr(auth), str(auth)):
        assert TOKEN not in text
        assert "svc@corp.invalid" not in text
        assert base64.b64encode(f"svc@corp.invalid:{TOKEN}".encode()).decode() not in text


def test_headers_returns_a_copy() -> None:
    auth = BasicAuthenticator("svc@corp.invalid", SecretStr(TOKEN))
    auth.headers()["Authorization"] = "tampered"
    assert auth.headers()["Authorization"].startswith("Basic ")
