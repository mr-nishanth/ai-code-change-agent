"""Gate D.2: the GitHub baseline reader against the git-backed fake GitHub (no network).

Every expectation about content, modes and hashes comes from real `git` (ls-tree, cat-file),
not from the reader's own arithmetic.
"""

import hashlib
import io
import logging
import tarfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_code_change_agent.application.git_providers.baseline import (
    MAX_SNAPSHOT_FILE_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SNAPSHOT_TOTAL_BYTES,
)
from ai_code_change_agent.application.workspaces.errors import BaselineError
from ai_code_change_agent.application.workspaces.paths import tree_hash
from ai_code_change_agent.integrations.github import baseline as reader_module
from tests.support.fake_github import Fault
from tests.support.github_repos import FILES, IDENTITY, ReaderRig, blob_bytes, git, reader_rig

READ_SCOPE = {"metadata": "read", "contents": "read"}


async def fails(rig: ReaderRig, code: str, *, sha: str | None = None, **identity: Any) -> None:
    with pytest.raises(BaselineError) as err:
        await rig.reader.read(replace(IDENTITY, **identity), sha or rig.sha)
    assert err.value.code == code, err.value.code
    assert "ghs_" not in str(err.value)


def root_tree(rig: ReaderRig) -> str:
    return git(rig.bare, "rev-parse", f"{rig.sha}^{{tree}}")


# ---- success ----------------------------------------------------------------------------------
async def test_pin_resolves_the_registered_default_branch_to_one_commit(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    assert await rig.reader.pin(IDENTITY) == rig.sha == git(rig.bare, "rev-parse", "main")


async def test_read_returns_every_file_verified_against_git(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, FILES | {"bin/run.sh": b"#!/bin/sh\necho hi\n"},
                     executable=("bin/run.sh",))  # fmt: skip
    baseline = await rig.reader.read(IDENTITY, rig.sha)
    expected = blob_bytes(rig.bare, rig.sha)
    assert baseline.commit_sha == rig.sha
    assert [f.path for f in baseline.files] == sorted(expected)
    for f in baseline.files:
        assert (f.content, f.executable) == expected[f.path]
    assert {f.path: f.executable for f in baseline.files}["bin/run.sh"] is True
    assert baseline.tree_hash == tree_hash(
        (p, x, hashlib.sha256(c).hexdigest()) for p, (c, x) in expected.items()
    )  # the existing platform tree-hash contract, from git's bytes
    assert baseline.total_bytes == sum(len(c) for c, _ in expected.values())


async def test_binary_and_prompt_injection_content_is_returned_byte_for_byte(
    tmp_path: Path,
) -> None:
    rig = reader_rig(tmp_path)
    files = {f.path: f.content for f in (await rig.reader.read(IDENTITY, rig.sha)).files}
    assert files["src/data.bin"] == FILES["src/data.bin"]
    assert files["README.md"] == FILES["README.md"]  # data only: never interpreted


async def test_the_reader_uses_a_read_only_token_and_cannot_write(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    await rig.reader.read(IDENTITY, rig.sha)
    assert [perms for perms, _ in rig.hub.token_meta.values()] == [READ_SCOPE]
    assert all(repos == {424242} for _, repos in rig.hub.token_meta.values())
    assert rig.hub.writes() == [("POST", "/app/installations/22/access_tokens")]
    (token,) = rig.hub.token_meta
    async with httpx.AsyncClient(transport=rig.hub.transport()) as c:  # the fake enforces scope
        r = await c.post("https://api.github.com/repos/acme/shop-service/git/blobs",
                         json={"content": "", "encoding": "base64"},
                         headers={"Authorization": f"Bearer {token}"})  # fmt: skip
    assert r.status_code == 403


async def test_the_download_never_carries_the_bearer_token(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    await rig.reader.read(IDENTITY, rig.sha)
    (download,) = rig.hub.downloads
    assert download.url.host == "codeload.github.com" and download.url.scheme == "https"
    assert "authorization" not in {k.lower() for k in download.headers}
    assert rig.hub.foreign_requests == []


async def test_every_operation_rechecks_installation_membership(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    await rig.reader.pin(IDENTITY)
    await rig.reader.read(IDENTITY, rig.sha)
    assert rig.hub.issued == 2  # a fresh exchange per operation, never a reused token
    rig.hub.accessible = set()  # removed from the App's selected repositories
    await fails(rig, "installation_access_denied")


async def test_a_truncated_listing_is_completed_by_a_bounded_walk(tmp_path: Path) -> None:
    files = {f"d{i}/sub/f{j}.txt": f"{i}-{j}".encode() for i in range(3) for j in range(2)}
    rig = reader_rig(tmp_path, files)
    rig.hub.truncate_recursive = True
    baseline = await rig.reader.read(IDENTITY, rig.sha)
    assert sorted(f.path for f in baseline.files) == sorted(files)
    tree_reads = [p for m, p in rig.hub.calls if "/git/trees/" in p]
    assert len(tree_reads) == 1 + 1 + 3 + 3  # recursive, root, three dirs, three subdirs


async def test_the_walk_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = reader_rig(tmp_path, {f"d{i}/f.txt": b"x" for i in range(3)})
    rig.hub.truncate_recursive = True
    monkeypatch.setattr(reader_module, "MAX_TREE_REQUESTS", 2)
    await fails(rig, "baseline_too_large")


# ---- repository identity and authorisation -----------------------------------------------------
@pytest.mark.parametrize(
    ("setup", "identity", "code"),
    [
        (lambda h: setattr(h, "accessible", set()), {}, "installation_access_denied"),
        (lambda h: None, {"provider_repository_id": "999"}, "installation_access_denied"),
        (lambda h: setattr(h, "installation_present", False), {}, "installation_missing"),
        (lambda h: setattr(h, "app_id", 999), {}, "app_jwt_invalid"),
        (lambda h: h.granted.update(contents="none"), {}, "installation_access_denied"),
        (lambda h: setattr(h, "full_name", "acme/renamed"), {}, "repository_identity_changed"),
        (lambda h: None, {"owner": "other"}, "repository_identity_changed"),
        (lambda h: setattr(h, "archived", True), {}, "repository_archived"),
    ],
)
async def test_identity_and_installation_problems_stop_the_read(
    tmp_path: Path, setup: Callable[[Any], None], identity: dict[str, str], code: str
) -> None:
    rig = reader_rig(tmp_path)
    setup(rig.hub)
    await fails(rig, code, **identity)
    assert rig.hub.downloads == []


async def test_pin_refuses_a_changed_default_branch(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.default_branch = "develop"
    with pytest.raises(BaselineError) as err:
        await rig.reader.pin(IDENTITY)
    assert err.value.code == "default_branch_changed"
    assert (await rig.reader.read(IDENTITY, rig.sha)).commit_sha == rig.sha  # pinned still reads


async def test_pin_refuses_a_missing_branch_and_a_malformed_ref(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    with pytest.raises(BaselineError) as err:
        await rig.reader.pin(replace(IDENTITY, default_branch="develop"))
    assert err.value.code == "default_branch_changed"
    rig.hub.faults.append(Fault("GET", r"/git/ref/heads/", status=404))
    with pytest.raises(BaselineError) as err:
        await rig.reader.pin(IDENTITY)
    assert err.value.code == "default_branch_missing"
    rig.hub.faults.append(Fault("GET", r"/git/ref/heads/", status=200,
                                body={"ref": "refs/heads/main", "object": {"sha": "zz"}}))  # fmt: skip
    with pytest.raises(BaselineError) as err:
        await rig.reader.pin(IDENTITY)
    assert err.value.code == "provider_response_invalid"


@pytest.mark.parametrize(
    "identity",
    [{"git_provider": "gitlab"}, {"provider_repository_id": "abc"}, {"owner": "a/b"},
     {"name": "x?y"}, {"default_branch": "main;rm"}, {"default_branch": "a/../b"}],
)  # fmt: skip
async def test_an_invalid_identity_is_refused_before_any_request(
    tmp_path: Path, identity: dict[str, str]
) -> None:
    rig = reader_rig(tmp_path)
    await fails(rig, "target_identity_invalid", **identity)
    with pytest.raises(BaselineError):
        await rig.reader.pin(replace(IDENTITY, **identity))
    assert rig.hub.calls == []


async def test_an_invalid_commit_sha_is_refused_before_any_request(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    await fails(rig, "baseline_request_invalid", sha="main")
    assert rig.hub.calls == []


# ---- commit and tree ---------------------------------------------------------------------------
async def test_a_missing_commit_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    await fails(rig, "base_commit_missing", sha="f" * 40)


async def test_a_commit_answer_for_another_sha_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", r"/git/commits/", status=200,
                                body={"sha": "0" * 40, "tree": {"sha": "1" * 40}}))  # fmt: skip
    await fails(rig, "baseline_mismatch")


async def test_a_listing_that_is_not_the_commits_tree_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.extra_tree_entries = [
        {
            "path": "ghost.txt",
            "mode": "100644",
            "type": "blob",
            "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
            "size": 0,
        }
    ]
    await fails(rig, "baseline_incomplete")  # the root tree SHA is recomputed locally


def listing(rig: ReaderRig, *items: dict[str, Any]) -> Fault:
    return Fault("GET", r"/git/trees/", status=200,
                 body={"sha": root_tree(rig), "tree": list(items), "truncated": False})  # fmt: skip


BLOB = {"mode": "100644", "type": "blob", "sha": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        "size": 0}  # fmt: skip


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([BLOB | {"path": "../escape"}], "unsafe_path"),
        ([BLOB | {"path": "/etc/passwd"}], "unsafe_path"),
        ([BLOB | {"path": "sub/.git/config"}], "unsafe_path"),
        ([BLOB | {"path": "a\\b"}], "unsafe_path"),
        ([BLOB | {"path": "x" * 256 + "/y"}], "unsafe_path"),
        ([BLOB | {"path": "a.txt"}, BLOB | {"path": "a.txt"}], "baseline_duplicate_path"),
        ([BLOB | {"path": "a.txt", "type": "tag"}], "unsupported_entry"),
        ([BLOB | {"path": "a.txt", "mode": "100664"}], "unsupported_entry"),
        ([{k: v for k, v in BLOB.items() if k != "size"} | {"path": "a.txt"}],
         "provider_response_invalid"),
        ([BLOB | {"path": "a.txt", "sha": "nothex"}], "provider_response_invalid"),
        (["not-an-object"], "provider_response_invalid"),
    ],
)  # fmt: skip
async def test_unsafe_or_malformed_entries_refuse_the_snapshot(
    tmp_path: Path, items: list[Any], code: str
) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(listing(rig, *items))
    await fails(rig, code)
    assert rig.hub.downloads == []


async def test_a_listing_with_a_wrong_shape_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", r"/git/trees/", status=200, body={"sha": "x", "tree": 1}))
    await fails(rig, "provider_response_invalid")


async def test_a_symlink_refuses_the_snapshot(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, FILES, symlinks={"link": "src/app.py"})
    await fails(rig, "unsupported_entry_symlink")


async def test_a_submodule_refuses_the_snapshot(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, FILES, gitlinks={"vendor/lib": "a" * 40})
    await fails(rig, "unsupported_entry_submodule")


async def test_paths_differing_only_in_case_refuse_the_snapshot(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, {"Docs/a.txt": b"1", "docs/b.txt": b"2"})
    await fails(rig, "path_case_collision")
    rig2 = reader_rig(tmp_path / "two", {"A.txt": b"1", "a.txt": b"2"})
    await fails(rig2, "path_case_collision")


# ---- limits ------------------------------------------------------------------------------------
def test_the_fixed_limits_are_the_approved_values() -> None:
    assert MAX_SNAPSHOT_FILES == 5_000
    assert MAX_SNAPSHOT_FILE_BYTES == 1024 * 1024
    assert MAX_SNAPSHOT_TOTAL_BYTES == 50 * 1024 * 1024


async def test_a_file_over_one_mib_refuses_the_snapshot(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, {"big.bin": b"x" * (MAX_SNAPSHOT_FILE_BYTES + 1)})
    await fails(rig, "baseline_file_too_large")


async def test_a_file_of_exactly_one_mib_is_accepted(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path, {"big.bin": b"x" * MAX_SNAPSHOT_FILE_BYTES})
    assert len((await rig.reader.read(IDENTITY, rig.sha)).files) == 1


async def test_too_many_files_refuse_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = reader_rig(tmp_path, {f"f{i}.txt": b"x" for i in range(4)})
    monkeypatch.setattr(reader_module, "MAX_SNAPSHOT_FILES", 3)
    await fails(rig, "baseline_too_many_files")


async def test_too_many_bytes_refuse_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = reader_rig(tmp_path, {"a.txt": b"x" * 6, "b.txt": b"y" * 6})
    monkeypatch.setattr(reader_module, "MAX_SNAPSHOT_TOTAL_BYTES", 10)
    await fails(rig, "baseline_too_large")


# ---- archive -----------------------------------------------------------------------------------
def retar(raw: bytes, change: Callable[[tarfile.TarFile, tarfile.TarFile], None],
          pax: dict[str, str] | None = None) -> bytes:  # fmt: skip
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as src, tarfile.open(
        fileobj=out, mode="w", format=tarfile.PAX_FORMAT,
        pax_headers=pax if pax is not None else src.pax_headers,
    ) as dst:  # fmt: skip
        change(src, dst)
    return out.getvalue()


def copy_all(skip: str = "", edit: str = "", mode: int | None = None) -> Callable[..., None]:
    def run(src: tarfile.TarFile, dst: tarfile.TarFile) -> None:
        for m in src.getmembers():
            if skip and m.name.endswith(skip):
                continue
            data = src.extractfile(m).read() if m.isreg() else None  # type: ignore[union-attr]
            if edit and m.name.endswith(edit) and data is not None:
                data = data[:-1] + b"!"
            if mode is not None and m.name.endswith("src/app.py"):
                m.mode = mode
            dst.addfile(m, io.BytesIO(data) if data is not None else None)

    return run


def prefix(rig: ReaderRig) -> str:
    return f"acme-shop-service-{rig.sha[:7]}/"


def add_member(name: str, kind: bytes = tarfile.REGTYPE, data: bytes = b"x") -> Callable[..., None]:
    def run(src: tarfile.TarFile, dst: tarfile.TarFile) -> None:
        copy_all()(src, dst)
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind == tarfile.SYMTYPE:
            info.linkname = "/etc/passwd"
            dst.addfile(info)
        else:
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))

    return run


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r, p: retar(r, copy_all(edit="src/app.py")), "archive_mismatch"),
        (lambda r, p: retar(r, copy_all(skip="src/app.py")), "archive_incomplete"),
        (lambda r, p: retar(r, copy_all(mode=0o775)), "archive_mismatch"),
        (lambda r, p: retar(r, add_member(p + "extra.txt")), "archive_unexpected_entry"),
        (lambda r, p: retar(r, add_member(p + "link", tarfile.SYMTYPE)),
         "archive_unexpected_entry"),
        (lambda r, p: retar(r, add_member("../evil.txt")), "archive_unexpected_entry"),
        (lambda r, p: retar(r, add_member("other-prefix/x")), "archive_unexpected_entry"),
        (lambda r, p: retar(r, add_member(p + "src/app.py", data=b"dup")),
         "archive_unexpected_entry"),
        (lambda r, p: retar(r, copy_all(), pax={"comment": "0" * 40}), "archive_mismatch"),
        (lambda r, p: b"this is not a tar archive" * 100, "archive_invalid"),
    ],
)  # fmt: skip
async def test_an_archive_that_does_not_match_the_tree_is_refused(
    tmp_path: Path, mutate: Callable[[bytes, str], bytes], code: str
) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.archive_mutator = lambda raw: mutate(raw, prefix(rig))
    await fails(rig, code)


async def test_export_ignore_makes_the_archive_incomplete_and_is_refused(tmp_path: Path) -> None:
    files = FILES | {".gitattributes": b"README.md export-ignore\n"}
    rig = reader_rig(tmp_path, files)
    await fails(rig, "archive_incomplete")  # never silently partial, no per-file fallback


async def test_export_subst_changes_content_and_is_refused(tmp_path: Path) -> None:
    files = FILES | {".gitattributes": b"version.txt export-subst\n",
                     "version.txt": b"$Format:%H$\n"}  # fmt: skip
    rig = reader_rig(tmp_path, files)
    await fails(rig, "archive_mismatch")


async def test_a_decompression_bomb_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.archive_mutator = lambda raw: b"\0" * (reader_module.MAX_UNPACKED_BYTES + 1024)
    await fails(rig, "baseline_too_large")


@pytest.mark.parametrize(
    "body",
    [lambda gz: gz[:-12], lambda gz: gz + b"trailing", lambda gz: b"\x1f\x8bnot gzip"],
)
async def test_a_truncated_or_corrupt_download_is_refused(
    tmp_path: Path, body: Callable[[bytes], bytes]
) -> None:
    rig = reader_rig(tmp_path)
    real = rig.hub.archive
    rig.hub.archive = lambda sha: body(real(sha))  # type: ignore[method-assign]
    await fails(rig, "archive_invalid")


async def test_an_oversize_download_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = reader_rig(tmp_path)
    monkeypatch.setattr(reader_module, "MAX_ARCHIVE_BYTES", 64)
    await fails(rig, "baseline_too_large")


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.com/acme/shop-service/legacy.tar.gz/x",
        "http://codeload.github.com/acme/shop-service/legacy.tar.gz/x",
        "https://user:pw@codeload.github.com/acme/shop-service/legacy.tar.gz/x",
        "https://codeload.github.com:8443/acme/shop-service/legacy.tar.gz/x",
        "https://codeload.github.com.evil.example/x",
        "",
    ],
)
async def test_only_the_fixed_download_host_is_followed(tmp_path: Path, location: str) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.download_location = location
    await fails(rig, "archive_redirect_refused")
    assert rig.hub.foreign_requests == [] and rig.hub.downloads == []


@pytest.mark.parametrize(("status", "code"), [(200, "archive_unavailable"),
                                              (404, "base_commit_missing")])  # fmt: skip
async def test_a_tarball_answer_without_redirect_is_refused(
    tmp_path: Path, status: int, code: str
) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.download_status = status
    if status == 404:
        rig.hub.faults.append(Fault("GET", r"/tarball/", status=404))
    await fails(rig, code)


async def test_a_failing_download_is_refused(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", "codeload", status=500))
    await fails(rig, "archive_unavailable")


# ---- reliability -------------------------------------------------------------------------------
async def test_a_read_timeout_is_retried_with_bounded_backoff(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", r"/repositories/", timeout=True))
    rig.hub.faults.append(Fault("GET", r"/git/commits/", status=502))
    assert (await rig.reader.read(IDENTITY, rig.sha)).commit_sha == rig.sha
    assert rig.sleeps == [0.5, 0.5]


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (Fault("GET", r"/repositories/", timeout=True, times=3), "provider_timeout"),
        (Fault("GET", r"/git/commits/", status=503, times=3), "provider_unavailable"),
    ],
)
async def test_retries_are_exhausted_and_reported(tmp_path: Path, fault: Fault, code: str) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(fault)
    await fails(rig, code)
    assert rig.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    "fault",
    [Fault("GET", r"/repositories/", status=429),
     Fault("GET", r"/git/trees/", status=403, headers={"x-ratelimit-remaining": "0"})],
)  # fmt: skip
async def test_rate_limits_fail_at_once_without_waiting(tmp_path: Path, fault: Fault) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(fault)
    await fails(rig, "provider_rate_limited")
    assert rig.sleeps == []


async def test_an_expired_token_is_refreshed_once(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", r"/repositories/", status=401))
    assert (await rig.reader.read(IDENTITY, rig.sha)).commit_sha == rig.sha
    assert rig.hub.issued == 2
    rig.hub.faults.append(Fault("GET", r"/repositories/", status=401, times=2))
    await fails(rig, "installation_auth_failed")


async def test_unexpected_statuses_are_typed_failures(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    rig.hub.faults.append(Fault("GET", r"/repositories/", status=404))
    await fails(rig, "repository_inaccessible")
    rig.hub.faults.append(Fault("GET", r"/repositories/", status=418))
    await fails(rig, "provider_unavailable")
    rig.hub.faults.append(Fault("GET", r"/repositories/", status=200, body={"id": 1}))
    await fails(rig, "target_identity_invalid")
    rig.hub.faults.append(Fault("GET", r"/git/trees/", status=404))
    await fails(rig, "baseline_incomplete")
    rig.hub.faults.append(Fault("GET", r"/git/commits/", status=418))
    await fails(rig, "provider_unavailable")


async def test_a_non_json_answer_is_a_typed_failure(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    real = rig.hub.handle

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repositories/"):
            return httpx.Response(200, content=b"<html>")
        return real(request)

    reader = reader_module.GitHubBaselineReader(
        auth=rig.auth, transport=httpx.MockTransport(broken)
    )
    with pytest.raises(BaselineError) as err:
        await reader.read(IDENTITY, rig.sha)
    assert err.value.code == "provider_response_invalid"


# ---- secrets -----------------------------------------------------------------------------------
async def test_no_token_or_key_material_reaches_logs_errors_or_repr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = reader_rig(tmp_path)
    await rig.reader.read(IDENTITY, rig.sha)
    rig.hub.accessible = set()
    await fails(rig, "installation_access_denied")
    text = caplog.text + repr(rig.reader) + repr(rig.auth)
    for token in rig.hub.tokens:
        assert token not in text
    assert "BEGIN" not in text and "PRIVATE" not in text
