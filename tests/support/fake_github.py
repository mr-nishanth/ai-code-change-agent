"""A fake GitHub REST API (and its codeload download host) for Gate D.1 and D.2 tests, served
through httpx.MockTransport.

Git objects are real: blobs, trees, commits and refs are created with the git CLI in a local bare
repository, so every SHA the adapter computes is checked against Git itself, not against a second
copy of the adapter's own arithmetic. Installation tokens are issued only for a JWT that verifies
against the App's public key. Nothing here opens a socket.
"""

import base64
import gzip
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ZERO = "0" * 40
DOWNLOAD_HOST = "codeload.github.com"
GRANTED = {"metadata": "read", "contents": "write", "pull_requests": "write"}


def make_key(path: Path, bits: int = 2048, mode: int = 0o600) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    path.chmod(mode)
    return key


@dataclass
class Fault:
    """Make one matching request fail. when="before": nothing is applied; "after": the request
    is applied, then the response is replaced (or a timeout raised)."""

    method: str
    path: str  # regex, searched in the request path
    when: str = "before"
    status: int | None = None
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: bool = False
    times: int = 1


class FakeGitHub:
    def __init__(
        self,
        bare: Path,
        *,
        public_key: rsa.RSAPublicKey,
        app_id: int = 11,
        installation_id: int = 22,
        owner: str = "acme",
        name: str = "shop-service",
        repo_id: int = 424242,
        default_branch: str = "main",
        clock: Callable[[], float] | None = None,
        token_ttl: int = 3600,
    ) -> None:
        self.bare = bare
        self.public_key = public_key
        self.app_id = app_id
        self.installation_id = installation_id
        self.owner, self.name, self.repo_id = owner, name, repo_id
        self.default_branch = default_branch
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())
        self.token_ttl = token_ttl
        self.granted = dict(GRANTED)
        self.accessible = {repo_id}
        self.installation_present = True
        self.archived = False
        self.drafts_supported = True
        self.hidden_commits: set[str] = set()
        self.extra_tree_entries: list[dict[str, Any]] = []
        self.faults: list[Fault] = []
        self.calls: list[tuple[str, str]] = []
        self.tokens: dict[str, float] = {}
        self.issued = 0
        self.pulls: dict[int, dict[str, Any]] = {}
        # Gate D.2 (reads)
        self.token_meta: dict[str, tuple[dict[str, str], set[int]]] = {}
        self.full_name: str | None = None  # override to simulate a rename or transfer
        self.truncate_recursive = False  # simulate a truncated recursive tree listing
        self.download_location: str | None = None  # override the tarball redirect target
        self.download_status = 302
        self.archive_mutator: Callable[[bytes], bytes] | None = None  # tamper with the tar
        self.downloads: list[httpx.Request] = []
        self.foreign_requests: list[httpx.Request] = []

    # ----- transport ---------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == DOWNLOAD_HOST:
            self.downloads.append(request)
            return self._download(request)
        if request.url.host != "api.github.com":
            self.foreign_requests.append(request)
            return httpx.Response(404)
        self.calls.append((request.method, path))
        fault = self._fault(request.method, path)
        if fault is not None and fault.when == "before":
            return self._fail(fault, request)
        response = self._route(request)
        if fault is not None:
            return self._fail(fault, request)
        return response

    def _fault(self, method: str, path: str) -> Fault | None:
        for f in self.faults:
            if f.times > 0 and f.method == method and re.search(f.path, path):
                f.times -= 1
                return f
        return None

    @staticmethod
    def _fail(fault: Fault, request: httpx.Request) -> httpx.Response:
        if fault.timeout:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(fault.status or 500, json=fault.body or {}, headers=fault.headers)

    # ----- routing -----------------------------------------------------------------------
    def _route(self, request: httpx.Request) -> httpx.Response:
        m, path = request.method, request.url.path
        tokens = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
        if tokens and m == "POST":
            return self._exchange(request, int(tokens.group(1)))
        token = self._authorised(request)
        if token is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        perms, token_repos = self.token_meta.get(token, ({}, set()))
        if m == "POST":
            needed = "pull_requests" if path.endswith("/pulls") else "contents"
            if perms.get(needed) != "write":
                return httpx.Response(403, json={"message": "Resource not accessible"})
        by_id = re.fullmatch(r"/repositories/(\d+)", path)
        if by_id and m == "GET":
            if int(by_id.group(1)) not in token_repos or int(by_id.group(1)) != self.repo_id:
                return httpx.Response(404, json={"message": "Not Found"})
            return self._repo()
        repo = f"/repos/{self.owner}/{self.name}"
        if not path.startswith(repo):
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(repo) :]
        body = request.read()
        data: dict[str, Any] = {} if not body else httpx.Response(200, content=body).json()
        routes: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            ("GET", r"", self._repo),
            ("GET", r"/git/commits/([0-9a-f]{40})", self._get_commit),
            ("GET", r"/git/trees/([0-9a-f]{40})", self._get_tree),
            ("GET", r"/git/blobs/([0-9a-f]{40})", self._get_blob),
            ("POST", r"/git/blobs", self._post_blob),
            ("POST", r"/git/trees", self._post_tree),
            ("POST", r"/git/commits", self._post_commit),
            ("POST", r"/git/refs", self._post_ref),
            ("GET", r"/git/ref/heads/(.+)", self._get_ref),
            ("GET", r"/pulls", self._list_pulls),
            ("POST", r"/pulls", self._post_pull),
            ("GET", r"/pulls/(\d+)", self._get_pull),
            ("GET", r"/tarball/([0-9a-f]{40})", self._tarball),
        ]
        for method, pattern, fn in routes:
            hit = re.fullmatch(pattern, rest)
            if hit and method == m:
                return fn(request, data, *hit.groups())
        return httpx.Response(405, json={"message": "not supported by the fake"})

    def _authorised(self, request: httpx.Request) -> str | None:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ")
        expiry = self.tokens.get(token)
        ok = expiry is not None and expiry > self.clock() and self.installation_present
        # Access is evaluated per request, as GitHub does for repositories removed later.
        repos = self.token_meta.get(token, ({}, set()))[1]
        return token if ok and repos <= self.accessible else None

    def _exchange(self, request: httpx.Request, installation: int) -> httpx.Response:
        assertion = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            claims = jwt.decode(
                assertion, self.public_key, algorithms=["RS256"],
                options={"require": ["iat", "exp", "iss"], "verify_exp": False,
                         "verify_iat": False, "verify_nbf": False},
            )  # fmt: skip
        except jwt.PyJWTError:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        now = self.clock()  # time is checked against the fake's clock, as GitHub does with its own
        if not claims["iat"] <= now < claims["exp"]:
            return httpx.Response(401, json={"message": "expired or not yet valid"})
        if claims["iss"] != str(self.app_id) or claims["exp"] - claims["iat"] > 600:
            return httpx.Response(401, json={"message": "bad issuer"})
        if installation != self.installation_id or not self.installation_present:
            return httpx.Response(404, json={"message": "Not Found"})
        wanted = httpx.Response(200, content=request.read()).json()
        repos = set(wanted.get("repository_ids") or [])
        for name in wanted.get("repositories") or []:
            if name.lower() != self.name.lower():
                return httpx.Response(422, json={"message": "repository not accessible"})
            repos.add(self.repo_id)
        if not repos <= self.accessible:
            return httpx.Response(422, json={"message": "repository not accessible"})
        rank = {"read": 1, "write": 2}
        for perm, level in (wanted.get("permissions") or {}).items():
            if rank.get(self.granted.get(perm, ""), 0) < rank[level]:
                return httpx.Response(422, json={"message": "permission not granted"})
        self.issued += 1
        token = f"ghs_FAKE{self.issued:032d}"
        expires = self.clock() + self.token_ttl
        self.tokens[token] = expires
        self.token_meta[token] = (dict(wanted.get("permissions") or {}), repos)
        return httpx.Response(
            201,
            json={
                "token": token,
                "expires_at": datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": wanted.get("permissions"),
                "repositories": [{"id": r} for r in sorted(repos)],
            },
        )

    # ----- repository and git database ---------------------------------------------------
    def _repo(self, *_: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": self.repo_id, "full_name": self.full_name or f"{self.owner}/{self.name}",
                "archived": self.archived, "disabled": False,
                "default_branch": self.default_branch, "private": True,
            },
        )  # fmt: skip

    def git(self, *args: str, data: bytes | None = None, env: dict[str, str] | None = None) -> str:
        full = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", **(env or {}),
        }  # fmt: skip
        out = subprocess.run(  # noqa: S603 - test helper, fixed git argv
            ["git", f"--git-dir={self.bare}", *args],  # noqa: S607
            input=data,
            env=full,
            capture_output=True,
            check=True,
        )
        return out.stdout.decode().strip()

    def _exists(self, sha: str, kind: str) -> bool:
        try:
            return self.git("cat-file", "-t", sha) == kind
        except subprocess.CalledProcessError:
            return False

    def _get_commit(self, _r: Any, _d: Any, sha: str) -> httpx.Response:
        if sha in self.hidden_commits or not self._exists(sha, "commit"):
            return httpx.Response(404, json={"message": "Not Found"})
        tree = self.git("rev-parse", f"{sha}^{{tree}}")
        parents = self.git("rev-list", "--parents", "-n1", sha).split()[1:]
        return httpx.Response(
            200, json={"sha": sha, "tree": {"sha": tree}, "parents": [{"sha": p} for p in parents]}
        )

    def _get_tree(self, request: httpx.Request, _d: Any, sha: str) -> httpx.Response:
        if not self._exists(sha, "tree"):
            return httpx.Response(404, json={"message": "Not Found"})
        recursive = request.url.params.get("recursive") == "1"
        out = self.git("ls-tree", *(["-r", "-t"] if recursive else []), "-l", "-z", sha)
        items: list[dict[str, Any]] = []
        for line in filter(None, out.split("\0")):
            meta, path = line.split("\t", 1)
            mode, kind, obj, size = meta.split()
            item: dict[str, Any] = {"path": path, "mode": mode, "type": kind, "sha": obj}
            if kind == "blob":
                item["size"] = int(size)
            items.append(item)
        items += self.extra_tree_entries
        if recursive and self.truncate_recursive:
            return httpx.Response(200, json={"sha": sha, "tree": items[:1], "truncated": True})
        return httpx.Response(200, json={"sha": sha, "tree": items, "truncated": False})

    def _tarball(self, _r: Any, _d: Any, sha: str) -> httpx.Response:
        if not self._exists(sha, "commit"):
            return httpx.Response(404, json={"message": "Not Found"})
        location = (
            self.download_location
            if self.download_location is not None
            else (
                f"https://{DOWNLOAD_HOST}/{self.owner}/{self.name}/legacy.tar.gz/{sha}?token=GRANT"
            )
        )
        return httpx.Response(self.download_status, headers={"location": location})

    def archive(self, sha: str) -> bytes:
        """The tarball exactly as git archive builds it (GitHub's tarballs come from it too)."""
        prefix = f"{self.owner}-{self.name}-{sha[:7]}/"
        raw = subprocess.run(  # noqa: S603 - test helper, fixed git argv
            ["git", f"--git-dir={self.bare}", "archive", "--format=tar", f"--prefix={prefix}", sha],  # noqa: S607
            capture_output=True, check=True, env={"PATH": os.environ.get("PATH", "")},
        ).stdout  # fmt: skip
        if self.archive_mutator is not None:
            raw = self.archive_mutator(raw)
        return gzip.compress(raw, mtime=0)

    def _download(self, request: httpx.Request) -> httpx.Response:
        hit = re.fullmatch(rf"/{self.owner}/{self.name}/legacy\.tar\.gz/([0-9a-f]{{40}})",
                           request.url.path)  # fmt: skip
        if hit is None or request.url.params.get("token") != "GRANT":
            return httpx.Response(404)
        fault = self._fault("GET", "codeload")
        if fault is not None:
            return self._fail(fault, request)
        return httpx.Response(200, content=self.archive(hit.group(1)))

    def _get_blob(self, _r: Any, _d: Any, sha: str) -> httpx.Response:
        if not self._exists(sha, "blob"):
            return httpx.Response(404, json={"message": "Not Found"})
        raw = subprocess.run(  # noqa: S603
            ["git", f"--git-dir={self.bare}", "cat-file", "blob", sha],  # noqa: S607
            capture_output=True,
            check=True,
            env={"PATH": os.environ.get("PATH", "")},
        ).stdout
        return httpx.Response(
            200, json={"sha": sha, "encoding": "base64", "content": base64.b64encode(raw).decode()}
        )

    def _post_blob(self, _r: Any, data: dict[str, Any]) -> httpx.Response:
        raw = base64.b64decode(data["content"])
        sha = self.git("hash-object", "-w", "--no-filters", "--stdin", data=raw)
        return httpx.Response(201, json={"sha": sha})

    def _post_tree(self, _r: Any, data: dict[str, Any]) -> httpx.Response:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.git("read-tree", data["base_tree"], env=env)
            lines = []
            for item in data["tree"]:
                if item["sha"] is None:
                    lines.append(f"0 {ZERO}\t{item['path']}")
                elif not self._exists(item["sha"], "blob"):
                    return httpx.Response(422, json={"message": "blob missing"})
                else:
                    lines.append(f"{item['mode']} {item['sha']}\t{item['path']}")
            self.git(
                "update-index", "--index-info", data=("\n".join(lines) + "\n").encode(), env=env
            )
            sha = self.git("write-tree", env=env)
        return httpx.Response(201, json={"sha": sha})

    def _post_commit(self, _r: Any, data: dict[str, Any]) -> httpx.Response:
        author, committer = data["author"], data["committer"]

        def stamp(who: dict[str, str]) -> str:
            moment = datetime.strptime(who["date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            return f"@{int(moment.timestamp())} +0000"

        env = {
            "GIT_AUTHOR_NAME": author["name"], "GIT_AUTHOR_EMAIL": author["email"],
            "GIT_AUTHOR_DATE": stamp(author), "GIT_COMMITTER_NAME": committer["name"],
            "GIT_COMMITTER_EMAIL": committer["email"], "GIT_COMMITTER_DATE": stamp(committer),
        }  # fmt: skip
        parents = [a for p in data["parents"] for a in ("-p", p)]
        sha = self.git(
            "commit-tree", data["tree"], *parents, data=data["message"].encode(), env=env
        )
        return httpx.Response(201, json={"sha": sha, "tree": {"sha": data["tree"]}})

    def ref(self, branch: str) -> str | None:
        try:
            return self.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
        except subprocess.CalledProcessError:
            return None

    def _post_ref(self, _r: Any, data: dict[str, Any]) -> httpx.Response:
        try:
            self.git("update-ref", data["ref"], data["sha"], ZERO)
        except subprocess.CalledProcessError:
            return httpx.Response(422, json={"message": "Reference already exists"})
        return httpx.Response(201, json={"ref": data["ref"], "object": {"sha": data["sha"]}})

    def _get_ref(self, _r: Any, _d: Any, branch: str) -> httpx.Response:
        sha = self.ref(branch)
        if sha is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            200, json={"ref": f"refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}
        )

    # ----- pull requests -----------------------------------------------------------------
    def add_pull(self, branch: str, *, state: str = "open", head_sha: str | None = None) -> int:
        number = len(self.pulls) + 1
        self.pulls[number] = {
            "number": number, "state": state, "draft": False, "title": "t", "body": "b",
            "head": {"ref": branch, "sha": head_sha or self.ref(branch), "repo": {"id": self.repo_id}},
            "base": {"ref": self.default_branch}, "merged_at": None, "merged": False,
            "html_url": f"https://github.com/{self.owner}/{self.name}/pull/{number}",
        }  # fmt: skip
        return number

    def _list_pulls(self, request: httpx.Request, _d: Any) -> httpx.Response:
        q = request.url.params
        head = q.get("head", "")
        branch = head.split(":", 1)[1] if ":" in head else head
        found = [
            p for p in self.pulls.values()
            if p["head"]["ref"] == branch and p["base"]["ref"] == q.get("base")
            and (q.get("state") == "all" or p["state"] == q.get("state", "open"))
        ]  # fmt: skip
        return httpx.Response(200, json=found)

    def _post_pull(self, _r: Any, data: dict[str, Any]) -> httpx.Response:
        if self.ref(data["head"]) is None:
            return httpx.Response(422, json={"message": "head invalid"})
        if any(
            p["head"]["ref"] == data["head"] and p["state"] == "open" for p in self.pulls.values()
        ):
            return httpx.Response(422, json={"message": "A pull request already exists"})
        if data.get("draft") and not self.drafts_supported:
            return httpx.Response(422, json={"message": "Draft pull requests are not supported"})
        number = self.add_pull(data["head"])
        self.pulls[number].update(
            {"draft": bool(data.get("draft")), "title": data["title"], "body": data["body"],
             "maintainer_can_modify": data.get("maintainer_can_modify")}
        )  # fmt: skip
        return httpx.Response(201, json=self.pulls[number])

    def _get_pull(self, _r: Any, _d: Any, number: str) -> httpx.Response:
        pr = self.pulls.get(int(number))
        if pr is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=pr)
