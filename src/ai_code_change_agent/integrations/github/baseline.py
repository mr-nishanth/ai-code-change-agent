"""GitHub App baseline reader: one exact commit of a registered repository, fully verified.

Flow (all GET except the token exchange; nothing is ever written):

1. A fresh read-only installation token for exactly the registered numeric repository id. GitHub
   refuses the exchange when the repository is not (or no longer) selected in the installation.
2. `GET /repositories/{id}`: the id, owner/name (registered identity), archived and disabled state
   and, when pinning, the registered default branch must all match.
3. Pin: `GET git/ref/heads/{default_branch}` gives one commit SHA (once per attempt).
4. Read: `GET git/commits/{sha}` (the SHA must match) and `GET git/trees/{tree}?recursive=1`; a
   truncated listing is completed with a bounded walk of non-recursive sub-tree requests. Every
   entry is checked (type, mode, path, size, duplicates, case collisions, limits) and the root
   tree SHA is recomputed locally from the entries: a missing, extra or altered entry fails.
5. Content: `GET tarball/{sha}` answers with a redirect to the fixed download host
   `codeload.github.com`, fetched **without** the bearer token, size-capped, decompressed with a
   hard output cap and parsed in memory (nothing is extracted to disk). Every archive member must
   be exactly one listed file with the listed mode, size and Git blob SHA; anything unexpected,
   missing or different fails the read. There is no per-file fallback.

Symlinks, submodules and other entry types refuse the whole snapshot. Limits: 5,000 files, 1 MiB
per file, 50 MiB total. Repository content is never executed or interpreted.
"""

import asyncio
import io
import re
import tarfile
import zlib
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from ai_code_change_agent.application.git_providers.baseline import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
    ProviderRepository,
    VerifiedBaseline,
)
from ai_code_change_agent.application.workspaces.errors import BaselineError, UnsafePathError
from ai_code_change_agent.application.workspaces.models import BaselineFile
from ai_code_change_agent.application.workspaces.paths import validate_relative_path
from ai_code_change_agent.integrations.github.authentication import (
    HEADERS,
    GitHubAppAuth,
    GitHubAuthError,
    rate_limited,
)
from ai_code_change_agent.integrations.github.gitobj import blob_sha, tree_sha

PROVIDER_KEY = "github"
API_BASE = "https://api.github.com"  # fixed: never from a Story, a model or the registry
DOWNLOAD_HOST = "codeload.github.com"  # the only redirect target ever followed
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # compressed
MAX_UNPACKED_BYTES = MAX_SNAPSHOT_TOTAL_BYTES + 16 * 1024 * 1024  # content plus tar headers
MAX_ARCHIVE_MEMBERS = 2 * MAX_SNAPSHOT_FILES + 16  # files, directories, the top-level folder
MAX_TREE_REQUESTS = 200
RETRIES = (0.5, 1.0)  # bounded retries of idempotent reads on timeout or 5xx
_SHA1 = re.compile(r"[0-9a-f]{40}")
_OWNER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_MODES = {"100644": False, "100755": True}
Sleep = Callable[[float], Awaitable[None]]


class _Entry:
    __slots__ = ("executable", "mode", "sha", "size")

    def __init__(self, mode: str, sha: str, size: int) -> None:
        self.mode, self.sha, self.size = mode, sha, size
        self.executable = _MODES[mode]


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise BaselineError("provider_response_invalid") from None


class _Session:
    """One client and one read-only token for one repository and one operation."""

    def __init__(
        self, client: httpx.AsyncClient, auth: GitHubAppAuth, repository_id: int, sleep: Sleep
    ) -> None:
        self.client = client
        self._auth = auth
        self._id = repository_id
        self._sleep = sleep
        self._fresh = True  # every operation starts with a new installation-membership check

    async def _token(self) -> str:
        try:
            token = await self._auth.token(self.client, self._id, scope="read", fresh=self._fresh)
        except GitHubAuthError as exc:
            raise BaselineError(exc.code) from None
        self._fresh = False
        return token

    async def get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        refreshed = False
        attempt = 0
        while True:
            token = await self._token()
            try:
                response = await self.client.get(
                    path, params=params, headers={**HEADERS, "Authorization": f"Bearer {token}"}
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < len(RETRIES):
                    await self._sleep(RETRIES[attempt])
                    attempt += 1
                    continue
                code = (
                    "provider_timeout"
                    if isinstance(exc, httpx.TimeoutException)
                    else ("provider_unavailable")
                )
                raise BaselineError(code) from None
            if response.status_code == 401 and not refreshed:
                self._auth.invalidate(self._id, "read")
                refreshed = True
                continue
            if response.status_code == 401:
                raise BaselineError("installation_auth_failed")
            if rate_limited(response):
                raise BaselineError("provider_rate_limited")  # never waited out here
            if response.status_code >= 500:
                if attempt < len(RETRIES):
                    await self._sleep(RETRIES[attempt])
                    attempt += 1
                    continue
                raise BaselineError("provider_unavailable")
            return response


class GitHubBaselineReader:
    def __init__(
        self,
        *,
        auth: GitHubAppAuth,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._auth = auth
        self._transport = transport
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._sleep = sleep

    # ----- port ----------------------------------------------------------------------------
    async def pin(self, repository: ProviderRepository) -> str:
        self._check(repository)
        async with self._client() as client:
            s = _Session(client, self._auth, int(repository.provider_repository_id), self._sleep)
            await self._verify_repository(s, repository, pinning=True)
            branch = quote(repository.default_branch, safe="/")
            response = await s.get(f"{self._repo(repository)}/git/ref/heads/{branch}")
            if response.status_code == 404:
                raise BaselineError("default_branch_missing")
            if response.status_code != 200:
                raise BaselineError("provider_unavailable")
            body = _json(response)
            obj = body.get("object") if isinstance(body, dict) else None
            sha = obj.get("sha") if isinstance(obj, dict) else None
            if (
                not isinstance(obj, dict)
                or obj.get("type") != "commit"
                or not isinstance(sha, str)
                or not _SHA1.fullmatch(sha)
                or body.get("ref") != f"refs/heads/{repository.default_branch}"
            ):
                raise BaselineError("provider_response_invalid")
            return sha

    async def read(self, repository: ProviderRepository, commit_sha: str) -> VerifiedBaseline:
        self._check(repository)
        if not _SHA1.fullmatch(commit_sha):
            raise BaselineError("baseline_request_invalid")
        async with self._client() as client:
            s = _Session(client, self._auth, int(repository.provider_repository_id), self._sleep)
            await self._verify_repository(s, repository, pinning=False)
            root = await self._commit_tree(s, repository, commit_sha)
            entries = await self._listing(s, repository, root)
            files = await self._archive(s, repository, commit_sha, entries)
        return VerifiedBaseline(commit_sha, files)

    # ----- identity --------------------------------------------------------------------------
    @staticmethod
    def _check(repository: ProviderRepository) -> None:
        if (
            repository.git_provider != PROVIDER_KEY
            or not repository.provider_repository_id.isdigit()
            or not _OWNER_NAME.fullmatch(repository.owner)
            or not _OWNER_NAME.fullmatch(repository.name)
            or not _BRANCH.fullmatch(repository.default_branch)
            or ".." in repository.default_branch
        ):
            raise BaselineError("target_identity_invalid")

    @staticmethod
    def _repo(repository: ProviderRepository) -> str:
        return f"/repos/{repository.owner}/{repository.name}"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        )

    @staticmethod
    async def _verify_repository(
        s: _Session, repository: ProviderRepository, *, pinning: bool
    ) -> None:
        response = await s.get(f"/repositories/{repository.provider_repository_id}")
        if response.status_code in (403, 404):
            raise BaselineError("repository_inaccessible")
        if response.status_code != 200:
            raise BaselineError("provider_unavailable")
        body = _json(response)
        if not isinstance(body, dict) or str(body.get("id")) != repository.provider_repository_id:
            raise BaselineError("target_identity_invalid")
        if str(body.get("full_name", "")).lower() != (
            f"{repository.owner}/{repository.name}".lower()
        ):
            raise BaselineError("repository_identity_changed")  # renamed or transferred
        if body.get("archived") is not False or body.get("disabled") is True:
            raise BaselineError("repository_archived")
        if pinning and body.get("default_branch") != repository.default_branch:
            raise BaselineError("default_branch_changed")

    # ----- commit and tree -------------------------------------------------------------------
    async def _commit_tree(self, s: _Session, repository: ProviderRepository, sha: str) -> str:
        response = await s.get(f"{self._repo(repository)}/git/commits/{sha}")
        if response.status_code in (404, 422):
            raise BaselineError("base_commit_missing")
        if response.status_code != 200:
            raise BaselineError("provider_unavailable")
        body = _json(response)
        tree = (body.get("tree") or {}).get("sha") if isinstance(body, dict) else None
        if body.get("sha") != sha or not isinstance(tree, str) or not _SHA1.fullmatch(tree):
            raise BaselineError("baseline_mismatch")
        return tree

    async def _tree(
        self, s: _Session, repository: ProviderRepository, sha: str, *, recursive: bool
    ) -> dict[str, Any]:
        response = await s.get(
            f"{self._repo(repository)}/git/trees/{sha}",
            params={"recursive": "1"} if recursive else None,
        )
        if response.status_code != 200:
            raise BaselineError(
                "baseline_incomplete"
                if response.status_code in (404, 409, 422)
                else "provider_unavailable"
            )
        body = _json(response)
        if (
            not isinstance(body, dict)
            or body.get("sha") != sha
            or not isinstance(body.get("tree"), list)
        ):
            raise BaselineError("provider_response_invalid")
        return body

    async def _listing(
        self, s: _Session, repository: ProviderRepository, root: str
    ) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        body = await self._tree(s, repository, root, recursive=True)
        if body.get("truncated") is False:
            for item in body["tree"]:
                self._add(entries, item, "")
        else:  # bounded breadth-first walk of non-recursive listings
            pending = [("", root)]
            requests = 1
            while pending:
                prefix, sha = pending.pop(0)
                requests += 1
                if requests > MAX_TREE_REQUESTS:
                    raise BaselineError("baseline_too_large")
                level = await self._tree(s, repository, sha, recursive=False)
                if level.get("truncated") is not False:
                    raise BaselineError("baseline_too_large")
                for item in level["tree"]:
                    sub = self._add(entries, item, prefix)
                    if sub is not None:
                        pending.append(sub)
        self._check_paths(entries)
        expected = tree_sha({p: (e.mode, e.sha) for p, e in entries.items()})
        if expected != root:
            raise BaselineError("baseline_incomplete")  # the listing is not the commit's tree
        return entries

    @staticmethod
    def _add(entries: dict[str, _Entry], item: Any, prefix: str) -> tuple[str, str] | None:
        if not isinstance(item, dict):
            raise BaselineError("provider_response_invalid")
        kind, mode, sha, path = (
            item.get("type"),
            item.get("mode"),
            item.get("sha"),
            item.get("path"),
        )
        if not isinstance(path, str) or not isinstance(sha, str) or not _SHA1.fullmatch(sha):
            raise BaselineError("provider_response_invalid")
        full = f"{prefix}{path}"
        try:
            if validate_relative_path(full) != full:
                raise BaselineError("unsafe_path")
        except UnsafePathError:
            raise BaselineError("unsafe_path") from None
        if kind == "tree" and mode == "040000":
            return f"{full}/", sha
        if kind == "commit" or mode == "160000":
            raise BaselineError("unsupported_entry_submodule")
        if mode == "120000":
            raise BaselineError("unsupported_entry_symlink")
        if kind != "blob" or mode not in _MODES:
            raise BaselineError("unsupported_entry")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BaselineError("provider_response_invalid")
        if size > MAX_SNAPSHOT_FILE_BYTES:
            raise BaselineError("baseline_file_too_large")
        if full in entries:
            raise BaselineError("baseline_duplicate_path")
        entries[full] = _Entry(mode, sha, size)
        if len(entries) > MAX_SNAPSHOT_FILES:
            raise BaselineError("baseline_too_many_files")
        return None

    @staticmethod
    def _check_paths(entries: dict[str, _Entry]) -> None:
        if sum(e.size for e in entries.values()) > MAX_SNAPSHOT_TOTAL_BYTES:
            raise BaselineError("baseline_too_large")
        seen: dict[str, str] = {}
        for path in entries:
            parts = path.split("/")
            if any(p.lower() == ".git" or len(p.encode()) > 255 for p in parts):
                raise BaselineError("unsafe_path")
            # Workspaces live on host file systems that may ignore case: two names that differ
            # only in case (files or directories) would silently collide.
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                key = prefix.casefold()
                if seen.setdefault(key, prefix) != prefix:
                    raise BaselineError("path_case_collision")

    # ----- content ---------------------------------------------------------------------------
    async def _archive(
        self, s: _Session, repository: ProviderRepository, sha: str, entries: dict[str, _Entry]
    ) -> tuple[BaselineFile, ...]:
        response = await s.get(f"{self._repo(repository)}/tarball/{sha}")
        if response.status_code == 404:
            raise BaselineError("base_commit_missing")
        if response.status_code not in (301, 302, 303, 307, 308):
            raise BaselineError("archive_unavailable")
        location = httpx.URL(response.headers.get("location", ""))
        if (
            location.scheme != "https"
            or location.host != DOWNLOAD_HOST
            or location.port not in (None, 443)
            or location.userinfo
        ):
            raise BaselineError("archive_redirect_refused")  # no other host is ever contacted
        packed = bytearray()
        try:
            # No Authorization header: the bearer token never leaves api.github.com. The download
            # URL carries GitHub's own short-lived grant and is never logged or stored.
            agent = {"User-Agent": HEADERS["User-Agent"], "Accept-Encoding": "identity"}
            async with s.client.stream("GET", location, headers=agent) as download:
                if download.status_code != 200:
                    raise BaselineError("archive_unavailable")
                if download.headers.get("content-encoding", "identity") != "identity":
                    raise BaselineError("archive_invalid")  # no transparent, uncapped inflation
                async for chunk in download.aiter_bytes():
                    packed += chunk
                    if len(packed) > MAX_ARCHIVE_BYTES:
                        raise BaselineError("baseline_too_large")
        except httpx.TimeoutException:
            raise BaselineError("provider_timeout") from None
        except httpx.HTTPError:
            raise BaselineError("archive_unavailable") from None
        return _verify_archive(bytes(packed), sha, entries)


def _gunzip(packed: bytes) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        raw = inflater.decompress(packed, MAX_UNPACKED_BYTES + 1)
    except zlib.error:
        raise BaselineError("archive_invalid") from None
    if len(raw) > MAX_UNPACKED_BYTES or inflater.unconsumed_tail:
        raise BaselineError("baseline_too_large")  # decompression bomb or oversize content
    if not inflater.eof or inflater.unused_data:
        raise BaselineError("archive_invalid")  # truncated or with trailing data
    return raw


def _verify_archive(
    packed: bytes, sha: str, entries: dict[str, _Entry]
) -> tuple[BaselineFile, ...]:
    raw = _gunzip(packed)
    files: dict[str, BaselineFile] = {}
    prefix: str | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            if tar.pax_headers.get("comment") != sha:
                raise BaselineError("archive_mismatch")  # git archive records the commit id
            for count, member in enumerate(tar, start=1):
                if count > MAX_ARCHIVE_MEMBERS:
                    raise BaselineError("baseline_too_large")
                top, _, rel = member.name.partition("/")
                if prefix is None:
                    prefix = top
                if top != prefix or not top or member.name.startswith("/"):
                    raise BaselineError("archive_unexpected_entry")
                rel = rel.rstrip("/") if member.isdir() else rel
                if member.isdir():
                    continue
                if not member.isreg():
                    raise BaselineError("archive_unexpected_entry")  # links, devices, fifos
                entry = entries.get(rel)
                if entry is None or rel in files:
                    raise BaselineError("archive_unexpected_entry")
                if member.size != entry.size or bool(member.mode & 0o100) != entry.executable:
                    raise BaselineError("archive_mismatch")
                handle = tar.extractfile(member)
                if handle is None:
                    raise BaselineError("archive_invalid")
                content = handle.read(entry.size + 1)
                if len(content) != entry.size or blob_sha(content) != entry.sha:
                    raise BaselineError("archive_mismatch")
                files[rel] = BaselineFile(rel, content, entry.executable)
    except (tarfile.TarError, EOFError, OSError, ValueError, UnicodeError):
        raise BaselineError("archive_invalid") from None
    if set(files) != set(entries):
        raise BaselineError("archive_incomplete")  # e.g. export-ignore: never silently partial
    return tuple(files[p] for p in sorted(files))
