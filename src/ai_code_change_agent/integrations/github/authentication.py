"""GitHub App authentication: a short-lived App JWT exchanged for an installation token that is
down-scoped to one repository and the least permissions the caller needs.

Scopes: `read` (metadata and contents read: baseline reading) and `write` (contents and pull
requests write: delivery). The reader never receives a write token. GitHub refuses the exchange if
the installation cannot reach the repository (for example it was removed from the App's selected
repositories) or lacks a permission, which makes every exchange a fresh installation-membership
check; the granted permissions and the exact repository set are verified again from the response.

Tokens live only in this object's memory and are never logged, persisted, put in a URL or handed
to another layer. One instance is shared per process (built once in the composition root).
"""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

import httpx
import jwt

from ai_code_change_agent.integrations.github.credentials import (
    GitHubAppCredentials,
)

JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540  # GitHub allows at most 600
TOKEN_REFRESH_MARGIN_SECONDS = 300
Scope = Literal["read", "write"]
SCOPES: dict[str, dict[str, str]] = {
    "read": {"metadata": "read", "contents": "read"},
    "write": {"metadata": "read", "contents": "write", "pull_requests": "write"},
}
METADATA_ONLY = {"metadata": "read"}
_RANK = {"read": 1, "write": 2}
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ai-code-change-agent",
}


class GitHubAuthError(Exception):
    """A fixed failure code; never a token, a key or a response body."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    return response.status_code == 403 and (
        response.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in response.headers
    )


class GitHubAppAuth:
    def __init__(
        self, credentials: GitHubAppCredentials, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._creds = credentials
        self._clock = clock
        self._tokens: dict[tuple[int, str], tuple[str, float]] = {}

    def __repr__(self) -> str:
        return "GitHubAppAuth(...)"

    @property
    def installation_id(self) -> int:
        return self._creds.installation_id

    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {
            "iat": now - JWT_BACKDATE_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": str(self._creds.app_id),
        }
        return jwt.encode(claims, self._creds.private_key, algorithm="RS256")

    def invalidate(self, repository_id: int, scope: Scope | None = None) -> None:
        for s in (scope,) if scope else ("read", "write"):
            self._tokens.pop((repository_id, s), None)

    async def token(
        self,
        client: httpx.AsyncClient,
        repository_id: int,
        *,
        scope: Scope = "write",
        fresh: bool = False,
    ) -> str:
        """A token for exactly one repository. `fresh` forces a new exchange (and so a new
        installation-membership check); callers use it once at the start of every operation."""
        key = (repository_id, scope)
        cached = self._tokens.get(key)
        if (
            not fresh
            and cached is not None
            and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > self._clock()
        ):
            return cached[0]
        self._tokens.pop(key, None)
        body = {"repository_ids": [repository_id], "permissions": SCOPES[scope]}
        token, expires, data = await self._exchange(client, body, SCOPES[scope])
        if _repository_ids(data) != {repository_id}:
            raise GitHubAuthError("repository_inaccessible")
        self._tokens[key] = (token, expires)
        return token

    async def name_token(self, client: httpx.AsyncClient, name: str) -> tuple[str, int]:
        """A metadata-only token for one repository of the installation's account, by name (for
        operator registration, before the numeric id is known). Never cached. Returns the token
        and the repository id GitHub granted."""
        token, _expires, data = await self._exchange(
            client, {"repositories": [name], "permissions": METADATA_ONLY}, METADATA_ONLY
        )
        ids = _repository_ids(data)
        if len(ids) != 1:
            raise GitHubAuthError("repository_inaccessible")
        return token, ids.pop()

    async def _exchange(
        self, client: httpx.AsyncClient, body: dict[str, Any], required: dict[str, str]
    ) -> tuple[str, float, dict[str, Any]]:
        path = f"/app/installations/{self._creds.installation_id}/access_tokens"
        try:
            response = await client.request(
                "POST",
                path,
                json=body,
                headers={**HEADERS, "Authorization": f"Bearer {self.app_jwt()}"},
            )
        except httpx.TimeoutException:
            raise GitHubAuthError("provider_timeout") from None
        except httpx.HTTPError:
            raise GitHubAuthError("provider_unavailable") from None
        status = response.status_code
        if rate_limited(response):
            raise GitHubAuthError("provider_rate_limited")
        if status == 401:
            raise GitHubAuthError("app_jwt_invalid")
        if status == 404:
            raise GitHubAuthError("installation_missing")
        if status == 422:
            # The repository is not (or no longer) selected in the installation, or a requested
            # permission is not granted: refused either way.
            raise GitHubAuthError("installation_access_denied")
        if status >= 500:
            raise GitHubAuthError("provider_unavailable")
        if status != 201:
            raise GitHubAuthError("installation_auth_failed")
        try:
            data = response.json()
            token = data["token"]
            expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
            granted = data.get("permissions") or {}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GitHubAuthError("installation_auth_failed") from None
        if not isinstance(token, str) or not token or not isinstance(granted, dict):
            raise GitHubAuthError("installation_auth_failed")
        for name, level in required.items():
            if _RANK.get(str(granted.get(name)), 0) < _RANK[level]:
                raise GitHubAuthError("permission_missing")
        return token, expires.timestamp(), data


def _repository_ids(data: dict[str, Any]) -> set[int]:
    try:
        return {int(r["id"]) for r in data.get("repositories") or []}
    except (KeyError, TypeError, ValueError):
        raise GitHubAuthError("installation_auth_failed") from None
