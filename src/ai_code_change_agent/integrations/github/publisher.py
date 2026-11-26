"""GitHub App publisher: verified branch, commit and draft pull request through the Git Database
and Pulls REST APIs, behind the existing DeliveryPublisher port.

Only verified inputs arrive here (registered identity, approved base commit, the patch's file
changes, the pinned snapshot, the deterministic branch and commit message, platform-generated PR
text). Every object GitHub creates is compared with a locally computed expectation. The only write
calls are POST to git/blobs, git/trees, git/commits, git/refs (create only) and pulls. There is no
ref update, no force, no merge, no delete and no write to the default branch. Remote state is
re-read before every write so a retry after an unknown outcome reconciles instead of duplicating.
"""

import base64
import hashlib
import re
from datetime import UTC
from typing import Any

import httpx

from ai_code_change_agent.application.delivery.errors import UNKNOWN_OUTCOME_CODE, DeliveryError
from ai_code_change_agent.application.delivery.naming import BRANCH
from ai_code_change_agent.application.git_providers.delivery import (
    DeliveryOutcome,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStep,
)
from ai_code_change_agent.application.workspaces.errors import UnsafePathError
from ai_code_change_agent.application.workspaces.models import ChangeOp
from ai_code_change_agent.application.workspaces.paths import (
    is_protected,
    tree_hash,
    validate_relative_path,
)
from ai_code_change_agent.integrations.github.authentication import (
    HEADERS,
    GitHubAppAuth,
    GitHubAuthError,
    rate_limited,
)
from ai_code_change_agent.integrations.github.gitobj import (
    blob_sha,
    commit_sha,
    tree_sha,
)

PROVIDER_KEY = "github"
API_BASE = "https://api.github.com"  # fixed: never from a Story, a model or the registry
AUTHOR = ("AI Code Change Agent", "noreply@ai-code-change-agent.invalid")  # as the local publisher
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SHA1 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REGULAR = {"100644": False, "100755": True}
_WRITE_PATHS = re.compile(r"/repos/[^/]+/[^/]+/(git/(blobs|trees|commits|refs)|pulls)")


class _Api:
    """One authenticated client for one repository. Reads that time out are retryable; writes that
    time out, fail at transport level or return 5xx have an unknown outcome."""

    def __init__(self, client: httpx.AsyncClient, auth: GitHubAppAuth, repository_id: int) -> None:
        self._client = client
        self._auth = auth
        self._repository_id = repository_id
        self._fresh = True  # the first call of every delivery re-checks installation membership

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        write = method != "GET"
        if write and (method != "POST" or not _WRITE_PATHS.fullmatch(path)):
            raise DeliveryError("delivery_request_invalid")  # no other write exists
        for first in (True, False):
            try:
                token = await self._auth.token(
                    self._client, self._repository_id, scope="write", fresh=self._fresh
                )
            except GitHubAuthError as exc:
                raise DeliveryError(exc.code) from None
            self._fresh = False
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers={**HEADERS, "Authorization": f"Bearer {token}"},
                )
            except httpx.TimeoutException:
                raise DeliveryError(UNKNOWN_OUTCOME_CODE if write else "provider_timeout") from None
            except httpx.HTTPError:
                raise DeliveryError(
                    UNKNOWN_OUTCOME_CODE if write else "provider_unavailable"
                ) from None
            if response.status_code == 401 and first:
                self._auth.invalidate(self._repository_id, "write")  # expired or revoked
                continue
            if response.status_code == 401:
                raise DeliveryError("installation_auth_failed")
            if rate_limited(response):
                raise DeliveryError("provider_rate_limited")
            if response.status_code >= 500:
                raise DeliveryError(UNKNOWN_OUTCOME_CODE if write else "provider_unavailable")
            return response
        raise DeliveryError("installation_auth_failed")  # pragma: no cover - loop always returns


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise DeliveryError("provider_unavailable") from None


class GitHubAppPublisher:
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

    # ----- entry point -------------------------------------------------------------------
    async def publish(self, request: DeliveryRequest) -> DeliveryReceipt:
        self._preflight(request)  # no I/O before every local rule passes
        steps: list[DeliveryStep] = []
        try:
            async with httpx.AsyncClient(
                base_url=API_BASE,
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                api = _Api(client, self._auth, int(request.target.provider_repository_id))
                return await self._deliver(api, request, steps)
        except DeliveryError as exc:
            branch_exists = any(s.name == "ref" and s.status != "UNKNOWN" for s in steps)
            outcome = (
                DeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN
                if exc.code == UNKNOWN_OUTCOME_CODE
                else DeliveryOutcome.GITHUB_BRANCH_PUBLISHED
                if branch_exists
                else DeliveryOutcome.DELIVERY_FAILED
            )
            return DeliveryReceipt(
                outcome,
                branch=request.branch if branch_exists else None,
                failure_code=exc.code,
                progress=tuple(steps),
            )

    def _preflight(self, request: DeliveryRequest) -> None:
        t = request.target
        if t.git_provider != PROVIDER_KEY:
            raise DeliveryError("target_identity_invalid")  # no silent provider fallback
        if not (_OWNER_NAME.fullmatch(t.owner) and _OWNER_NAME.fullmatch(t.name)):
            raise DeliveryError("target_identity_invalid")
        if not t.provider_repository_id.isdigit() or not _OWNER_NAME.fullmatch(t.default_branch):
            raise DeliveryError("target_identity_invalid")
        if not BRANCH.fullmatch(request.branch) or not _SHA1.fullmatch(request.base_commit_sha):
            raise DeliveryError("delivery_request_invalid")
        if request.pull_request is None or not request.baseline or not request.changes:
            raise DeliveryError("delivery_request_invalid")
        if not _HEX64.fullmatch(request.result_tree_hash):
            raise DeliveryError("delivery_request_invalid")
        for change in request.changes:
            try:
                if validate_relative_path(change.path) != change.path:
                    raise DeliveryError("unsafe_path")
            except UnsafePathError:
                raise DeliveryError("unsafe_path") from None
            if is_protected(change.path):
                raise DeliveryError("protected_path")
            if (change.op is ChangeOp.DELETE) != (change.content is None):
                raise DeliveryError("delivery_request_invalid")

    # ----- the delivery ------------------------------------------------------------------
    async def _deliver(
        self, api: _Api, request: DeliveryRequest, steps: list[DeliveryStep]
    ) -> DeliveryReceipt:
        t = request.target
        repo = f"/repos/{t.owner}/{t.name}"
        await self._verify_repository(api, repo, request)
        steps.append(DeliveryStep("repository", "VERIFIED", t.provider_repository_id))
        base_tree, entries = await self._verify_base(api, repo, request)
        steps.append(DeliveryStep("base", "VERIFIED", request.base_commit_sha))

        result = dict(entries)
        platform = {b.path: (b.executable, b.sha256) for b in request.baseline}
        for c in request.changes:
            if c.op is ChangeOp.DELETE:
                result.pop(c.path)
                platform.pop(c.path)
                continue
            data = c.content or b""
            mode = entries[c.path][0] if c.op is ChangeOp.MODIFY else "100644"
            result[c.path] = (mode, blob_sha(data))
            platform[c.path] = (mode == "100755", hashlib.sha256(data).hexdigest())
        if tree_hash((p, e, s) for p, (e, s) in platform.items()) != request.result_tree_hash:
            raise DeliveryError("delivery_tree_mismatch")  # the approved result, before any write
        expected_tree = tree_sha(result)
        expected_commit = commit_sha(
            tree=expected_tree,
            parent=request.base_commit_sha,
            name=AUTHOR[0],
            email=AUTHOR[1],
            timestamp=int(request.commit_time.timestamp()),
            message=request.commit_message,
        )

        existing = await self._get_ref(api, repo, request.branch)
        if existing is not None:
            if existing != expected_commit:
                raise DeliveryError("branch_conflict")  # never overwritten
            steps.append(DeliveryStep("ref", "REUSED", expected_commit))
        else:
            await self._write_objects(
                api, repo, request, entries, base_tree, expected_tree, expected_commit, steps
            )
            await self._create_ref(api, repo, request.branch, expected_commit, steps)

        number, url, head, created = await self._pull_request(
            api, repo, request, expected_commit, steps
        )
        return DeliveryReceipt(
            DeliveryOutcome.PULL_REQUEST_CREATED,
            branch=request.branch,
            commit_sha=expected_commit,
            tree_hash=request.result_tree_hash,
            created=created,
            pr_id=str(number),
            pr_url=url,
            pr_number=number,
            pr_head_sha=head,
            base_branch=t.default_branch,
            progress=tuple(steps),
        )

    async def _verify_repository(self, api: _Api, repo: str, request: DeliveryRequest) -> None:
        t = request.target
        response = await api.call("GET", repo)
        if response.status_code in (403, 404):
            raise DeliveryError("repository_inaccessible")
        if response.status_code != 200:
            raise DeliveryError("provider_unavailable")
        body = _json(response)
        if str(body.get("id")) != t.provider_repository_id:
            raise DeliveryError("target_identity_invalid")
        if body.get("archived") is not False or body.get("disabled") is True:
            raise DeliveryError("repository_inaccessible")
        if body.get("default_branch") != t.default_branch:
            raise DeliveryError("base_branch_not_allowed")  # only the registered default branch

    async def _verify_base(
        self, api: _Api, repo: str, request: DeliveryRequest
    ) -> tuple[str, dict[str, tuple[str, str]]]:
        response = await api.call("GET", f"{repo}/git/commits/{request.base_commit_sha}")
        if response.status_code in (404, 422):
            raise DeliveryError("base_commit_missing")
        if response.status_code != 200:
            raise DeliveryError("provider_unavailable")
        commit = _json(response)
        base_tree = str((commit.get("tree") or {}).get("sha", ""))
        if commit.get("sha") != request.base_commit_sha or not _SHA1.fullmatch(base_tree):
            raise DeliveryError("baseline_mismatch")
        response = await api.call("GET", f"{repo}/git/trees/{base_tree}", params={"recursive": "1"})
        if response.status_code != 200:
            raise DeliveryError("provider_unavailable")
        listing = _json(response)
        if listing.get("truncated") is not False:
            raise DeliveryError("baseline_too_large")
        entries: dict[str, tuple[str, str]] = {}
        for item in listing.get("tree") or []:
            if item.get("type") == "tree":
                continue
            mode, sha = str(item.get("mode")), str(item.get("sha"))
            if item.get("type") != "blob" or mode not in _REGULAR or not _SHA1.fullmatch(sha):
                raise DeliveryError("baseline_mismatch")  # symlinks, submodules: not in Gate C
            entries[str(item.get("path"))] = (mode, sha)
        baseline = {b.path: b for b in request.baseline}
        if set(entries) != set(baseline) or any(
            _REGULAR[entries[p][0]] != baseline[p].executable for p in entries
        ):
            raise DeliveryError("baseline_mismatch")
        for c in request.changes:  # the bytes the patch was validated against
            if c.op is ChangeOp.CREATE:
                if c.path in entries:
                    raise DeliveryError("baseline_mismatch")
                continue
            if c.path not in entries:
                raise DeliveryError("baseline_mismatch")
            blob = await api.call("GET", f"{repo}/git/blobs/{entries[c.path][1]}")
            if blob.status_code != 200:
                raise DeliveryError("provider_unavailable")
            body = _json(blob)
            try:
                data = base64.b64decode(str(body.get("content", "")), validate=False)
            except ValueError:
                raise DeliveryError("baseline_mismatch") from None
            if (
                body.get("encoding") != "base64"
                or blob_sha(data) != entries[c.path][1]
                or hashlib.sha256(data).hexdigest() != baseline[c.path].sha256
            ):
                raise DeliveryError("baseline_mismatch")
        return base_tree, entries

    async def _write(
        self, api: _Api, name: str, path: str, body: dict[str, Any], steps: list[DeliveryStep]
    ) -> httpx.Response:
        try:
            response = await api.call("POST", path, json=body)
        except DeliveryError as exc:
            if exc.code == UNKNOWN_OUTCOME_CODE:
                steps.append(DeliveryStep(name, "UNKNOWN"))
            raise
        if response.status_code == 403:
            raise DeliveryError("permission_missing")
        return response

    async def _write_objects(
        self,
        api: _Api,
        repo: str,
        request: DeliveryRequest,
        entries: dict[str, tuple[str, str]],
        base_tree: str,
        expected_tree: str,
        expected_commit: str,
        steps: list[DeliveryStep],
    ) -> None:
        items: list[dict[str, Any]] = []
        for c in request.changes:
            if c.op is ChangeOp.DELETE:
                items.append(
                    {"path": c.path, "mode": entries[c.path][0], "type": "blob", "sha": None}
                )
                continue
            data = c.content or b""
            response = await self._write(
                api,
                "blobs",
                f"{repo}/git/blobs",
                {"content": base64.b64encode(data).decode(), "encoding": "base64"},
                steps,
            )
            if response.status_code != 201 or _json(response).get("sha") != blob_sha(data):
                raise DeliveryError("delivery_tree_mismatch")
            mode = entries[c.path][0] if c.op is ChangeOp.MODIFY else "100644"
            items.append({"path": c.path, "mode": mode, "type": "blob", "sha": blob_sha(data)})
        steps.append(DeliveryStep("blobs", "CREATED", str(len(items))))
        response = await self._write(
            api, "tree", f"{repo}/git/trees", {"base_tree": base_tree, "tree": items}, steps
        )
        if response.status_code != 201 or _json(response).get("sha") != expected_tree:
            raise DeliveryError("delivery_tree_mismatch")
        steps.append(DeliveryStep("tree", "CREATED", expected_tree))
        who = {
            "name": AUTHOR[0],
            "email": AUTHOR[1],
            "date": request.commit_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        response = await self._write(
            api,
            "commit",
            f"{repo}/git/commits",
            {
                "message": request.commit_message,
                "tree": expected_tree,
                "parents": [request.base_commit_sha],
                "author": who,
                "committer": who,
            },
            steps,
        )
        body = _json(response) if response.status_code == 201 else {}
        if (body.get("tree") or {}).get("sha") != expected_tree:
            raise DeliveryError("delivery_tree_mismatch")
        if body.get("sha") != expected_commit:
            raise DeliveryError("commit_mismatch")  # metadata differs; no ref is created
        steps.append(DeliveryStep("commit", "CREATED", expected_commit))

    async def _get_ref(self, api: _Api, repo: str, branch: str) -> str | None:
        response = await api.call("GET", f"{repo}/git/ref/heads/{branch}")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise DeliveryError("provider_unavailable")
        body = _json(response)
        if body.get("ref") != f"refs/heads/{branch}":
            raise DeliveryError("branch_conflict")
        return str((body.get("object") or {}).get("sha"))

    async def _create_ref(
        self, api: _Api, repo: str, branch: str, commit: str, steps: list[DeliveryStep]
    ) -> None:
        response = await self._write(
            api, "ref", f"{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit}, steps
        )
        if response.status_code == 201:
            if (_json(response).get("object") or {}).get("sha") != commit:
                raise DeliveryError("branch_conflict")
            steps.append(DeliveryStep("ref", "CREATED", commit))
            return
        if response.status_code == 422:  # created meanwhile: reuse only the identical commit
            existing = await self._get_ref(api, repo, branch)
            if existing != commit:
                raise DeliveryError("branch_conflict")
            steps.append(DeliveryStep("ref", "REUSED", commit))
            return
        raise DeliveryError("provider_unavailable")

    async def _find_pull_request(
        self, api: _Api, repo: str, request: DeliveryRequest, commit: str
    ) -> int | None:
        t = request.target
        response = await api.call(
            "GET",
            f"{repo}/pulls",
            params={
                "head": f"{t.owner}:{request.branch}",
                "base": t.default_branch,
                "state": "all",
                "per_page": "100",
            },
        )
        if response.status_code != 200:
            raise DeliveryError("provider_unavailable")
        found = [p for p in _json(response) if (p.get("head") or {}).get("ref") == request.branch]
        if not found:
            return None
        for pr in found:
            if self._matches(pr, request, commit):
                return int(pr["number"])
        raise DeliveryError("pull_request_conflict")  # closed, merged or another head: not reused

    @staticmethod
    def _matches(pr: dict[str, Any], request: DeliveryRequest, commit: str) -> bool:
        head, base = pr.get("head") or {}, pr.get("base") or {}
        return (
            pr.get("state") == "open"
            and pr.get("merged_at") is None
            and head.get("ref") == request.branch
            and head.get("sha") == commit
            and str((head.get("repo") or {}).get("id")) == request.target.provider_repository_id
            and base.get("ref") == request.target.default_branch
        )

    async def _pull_request(
        self,
        api: _Api,
        repo: str,
        request: DeliveryRequest,
        commit: str,
        steps: list[DeliveryStep],
    ) -> tuple[int, str, str, bool]:
        spec = request.pull_request
        if spec is None:  # pragma: no cover - rejected in preflight
            raise DeliveryError("delivery_request_invalid")
        number = await self._find_pull_request(api, repo, request, commit)
        created = False
        if number is None:
            response = await self._write(
                api,
                "pull_request",
                f"{repo}/pulls",
                {
                    "title": spec.title,
                    "body": spec.body,
                    "head": request.branch,
                    "base": request.target.default_branch,
                    "draft": spec.draft,
                    "maintainer_can_modify": False,
                },
                steps,
            )
            if response.status_code == 201:
                number, created = int(_json(response)["number"]), True
            elif response.status_code == 422:  # an open PR may already exist for this head
                number = await self._find_pull_request(api, repo, request, commit)
                if number is None:
                    raise DeliveryError("pull_request_rejected")  # e.g. drafts not available
            else:
                raise DeliveryError("pull_request_rejected")
        steps.append(DeliveryStep("pull_request", "CREATED" if created else "REUSED", str(number)))
        # Only a re-read proves what exists now.
        response = await api.call("GET", f"{repo}/pulls/{number}")
        if response.status_code != 200:
            raise DeliveryError(UNKNOWN_OUTCOME_CODE if created else "provider_unavailable")
        pr = _json(response)
        if not self._matches(pr, request, commit) or pr.get("merged") is True:
            raise DeliveryError("pull_request_conflict")
        t = request.target
        url = str(pr.get("html_url", ""))
        if url.lower() != f"https://github.com/{t.owner}/{t.name}/pull/{number}".lower():
            raise DeliveryError("delivery_receipt_invalid")
        return number, url, commit, created
