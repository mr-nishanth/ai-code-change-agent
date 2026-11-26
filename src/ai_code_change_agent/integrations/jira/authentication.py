"""Jira Cloud REST authentication (email + API token). Replaceable by an OAuth implementation."""

import base64
from typing import Protocol

from pydantic import SecretStr


class JiraAuthenticator(Protocol):
    def headers(self) -> dict[str, str]: ...


class BasicAuthenticator:
    """HTTP Basic with the Atlassian account e-mail and an API token (not the webhook secret)."""

    def __init__(self, email: str, api_token: SecretStr) -> None:
        raw = f"{email}:{api_token.get_secret_value()}".encode()
        self._header = "Basic " + base64.b64encode(raw).decode("ascii")

    def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}

    def __repr__(self) -> str:
        return "BasicAuthenticator(<redacted>)"

    __str__ = __repr__
