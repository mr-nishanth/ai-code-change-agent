"""Read-only registry helper: describes a repository of the App installation by owner/name.

Used only by operator registration (`repositories register`, also with `--from-provider`). It
requests a metadata-only installation token for that one repository name: GitHub refuses it when
the repository is not selected in the installation. It never activates, maps or profiles
anything; the registry service stores the repository DISABLED.
"""

import re

import httpx

from ai_code_change_agent.application.git_providers.ports import ProviderRepositoryInfo
from ai_code_change_agent.domain.repositories.errors import ProviderMismatchError
from ai_code_change_agent.integrations.github.authentication import (
    HEADERS,
    GitHubAppAuth,
    GitHubAuthError,
)

API_BASE = "https://api.github.com"
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class GitHubAppRepositoryClient:
    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._auth = auth
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    async def describe_repository(
        self, *, owner: str, name: str, access_reference: str
    ) -> ProviderRepositoryInfo:
        if access_reference != f"app-installation:{self._auth.installation_id}":
            raise ProviderMismatchError("access_reference_not_this_installation")
        if not (_OWNER_NAME.fullmatch(owner) and _OWNER_NAME.fullmatch(name)):
            raise ProviderMismatchError("provider_identity_mismatch")
        async with httpx.AsyncClient(
            base_url=API_BASE,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            try:
                token, repository_id = await self._auth.name_token(client, name)
                response = await client.get(
                    f"/repositories/{repository_id}",
                    headers={**HEADERS, "Authorization": f"Bearer {token}"},
                )
            except GitHubAuthError as exc:
                raise ProviderMismatchError(exc.code) from None
            except httpx.HTTPError:
                raise ProviderMismatchError("provider_unavailable") from None
        if response.status_code != 200:
            raise ProviderMismatchError("repository_inaccessible")
        try:
            body = response.json()
            full_owner, full_name = str(body["full_name"]).split("/", 1)
            info = ProviderRepositoryInfo(
                provider_repository_id=str(int(body["id"])),
                owner=full_owner,
                name=full_name,
                default_branch=str(body["default_branch"]),
            )
            archived = body.get("archived") is not False or body.get("disabled") is True
        except (ValueError, KeyError, TypeError):
            raise ProviderMismatchError("provider_response_invalid") from None
        if info.provider_repository_id != str(repository_id):
            raise ProviderMismatchError("provider_identity_mismatch")
        if info.owner.lower() != owner.lower() or info.name.lower() != name.lower():
            raise ProviderMismatchError("provider_identity_mismatch")  # other account or renamed
        if archived:
            raise ProviderMismatchError("repository_archived")
        return info
