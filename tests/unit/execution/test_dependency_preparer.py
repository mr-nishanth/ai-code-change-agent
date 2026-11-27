"""Trusted dependency preparation with a fake Docker: wheels only, hash-locked, offline install,
immutable cached bundles, fail closed."""

import hashlib
import json
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_code_change_agent.domain.dependencies.policy import (
    DependencyPolicyError,
    DependencyRequest,
    validate_requests,
)
from ai_code_change_agent.execution.docker.dependencies import DockerDependencyPreparer
from ai_code_change_agent.execution.docker.process import ProcessResult
from tests.support.dependency_fakes import TEST_NETWORK, TEST_PROXY, PublishedVerifier, StaticEgress

IMAGE = "ai-code-change-agent/execution-python:0.2.0"


def mounts(argv: Sequence[str]) -> dict[str, str]:
    out = {}
    for i, a in enumerate(argv):
        if a == "--mount":
            parts = dict(p.split("=", 1) for p in argv[i + 1].split(",") if "=" in p)
            out[parts["dst"]] = parts["src"] + (",readonly" if "readonly" in argv[i + 1] else "")
    return out


class FakeDocker:
    """Simulates the resolver (writes wheels into /wheels) and the installer (fills /site)."""

    def __init__(self, files: dict[str, bytes] | None = None, *, fail: bytes | None = None) -> None:
        self.files = files if files is not None else {"flask-3.1.0-py3-none-any.whl": b"FLASK"}
        self.fail = fail
        self.calls: list[list[str]] = []
        self.image_id = b"sha256:" + b"1" * 64 + b"\n"

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        argv = list(argv)
        if argv[1:3] == ["image", "inspect"]:
            return ProcessResult(
                exit_code=0, stdout=self.image_id, stderr=b"", truncated=False, timed_out=False
            )
        self.calls.append(argv)
        m = mounts(argv)
        if "download" in argv:
            if self.fail is not None:
                return ProcessResult(
                    exit_code=1, stdout=b"", stderr=self.fail, truncated=False, timed_out=False
                )
            for name, data in self.files.items():
                (Path(m["/wheels"]) / name).write_bytes(data)
        else:
            (Path(m["/site"]) / "flask").mkdir()
            (Path(m["/site"]) / "flask" / "__init__.py").write_text("")
        return ProcessResult(exit_code=0, stdout=b"", stderr=b"", truncated=False, timed_out=False)


def preparer(tmp_path: Path, docker: FakeDocker, **kw: object) -> DockerDependencyPreparer:
    return DockerDependencyPreparer(
        root=tmp_path / "deps",
        image=IMAGE,
        index_url="https://pypi.org/simple",
        runner=docker,
        egress=kw.pop("egress", StaticEgress()),  # type: ignore[arg-type]
        verifier=kw.pop("verifier", PublishedVerifier()),  # type: ignore[arg-type]
        uid=1000,
        gid=1000,
        **kw,  # type: ignore[arg-type]
    )


def req(*names: str):  # type: ignore[no-untyped-def]
    return validate_requests(list(names), max_packages=10)


async def test_a_wheel_whose_published_hash_differs_is_never_published(tmp_path: Path) -> None:
    verifier = PublishedVerifier({"flask-3.1.0-py3-none-any.whl": "f" * 64})
    with pytest.raises(DependencyPolicyError, match="dependency_hash_mismatch"):
        await preparer(tmp_path, FakeDocker(), verifier=verifier).prepare(req("flask==3.1.0"))
    assert [p.name for p in (tmp_path / "deps").iterdir() if not p.name.startswith(".")] == []


async def test_unpublished_metadata_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_metadata_unavailable"):
        await preparer(tmp_path, FakeDocker(), verifier=PublishedVerifier({})).prepare(
            req("flask==3.1.0")
        )


async def test_a_cached_bundle_without_verification_records_is_rebuilt(tmp_path: Path) -> None:
    docker = FakeDocker()
    bundle = await preparer(tmp_path, docker).prepare(req("flask==3.1.0"))
    assert bundle is not None
    final = tmp_path / "deps" / bundle.key
    meta = final / "lock.json"
    final.chmod(0o755)
    meta.chmod(0o644)
    data = json.loads(meta.read_text())
    data.pop("verification")
    meta.write_text(json.dumps(data))
    verifier = PublishedVerifier()
    again = await preparer(tmp_path, FakeDocker(), verifier=verifier).prepare(req("flask==3.1.0"))
    assert again is not None and verifier.calls == 1  # rebuilt and cross-checked again
    assert [p for p in (tmp_path / "deps").iterdir() if p.name.startswith(".corrupt-")]


async def test_stale_build_directories_of_a_dead_worker_are_swept(tmp_path: Path) -> None:
    import os

    root = tmp_path / "deps"
    stale = root / ".tmp-dead-worker"
    fresh = root / ".tmp-live-worker"
    for d in (stale, fresh):
        (d / "wheels").mkdir(parents=True)
    os.utime(stale, (0, 0))
    await preparer(tmp_path, FakeDocker(), timeout_seconds=10).prepare(req("flask==3.1.0"))
    assert not stale.exists() and fresh.exists()


async def test_nothing_requested_needs_no_bundle(tmp_path: Path) -> None:
    docker = FakeDocker()
    assert await preparer(tmp_path, docker).prepare(()) is None
    assert docker.calls == []


async def test_a_bundle_is_locked_installed_offline_frozen_and_reused(tmp_path: Path) -> None:
    docker = FakeDocker()
    prep = preparer(tmp_path, docker)
    bundle = await prep.prepare(req("flask==3.1.0"))
    assert bundle is not None
    (pkg,) = bundle.packages
    assert (pkg.name, pkg.version, pkg.sha256) == (
        "flask",
        "3.1.0",
        hashlib.sha256(b"FLASK").hexdigest(),
    )
    final = tmp_path / "deps" / bundle.key
    lock = (final / "wheels" / "requirements.lock").read_text()
    assert lock == f"flask==3.1.0 --hash=sha256:{pkg.sha256}\n"
    assert bundle.lock_sha256 == hashlib.sha256(lock.encode()).hexdigest()
    assert bundle.site_dir == str(final / "site") and (final / "site" / "flask").is_dir()
    assert not (final.stat().st_mode & stat.S_IWUSR)  # immutable
    resolver, installer = docker.calls
    assert resolver[resolver.index("--network") + 1] == TEST_NETWORK  # internal, never bridge
    assert resolver[resolver.index("--proxy") + 1] == TEST_PROXY
    assert f"HTTPS_PROXY={TEST_PROXY}" in resolver
    assert "--only-binary=:all:" in resolver and resolver[resolver.index("--index-url") + 1] == (
        "https://pypi.org/simple"
    )
    assert mounts(resolver)["/req"].endswith(",readonly")
    assert installer[installer.index("--network") + 1] == "none"
    assert {"--no-index", "--require-hashes", "--no-deps"} <= set(installer)
    assert mounts(installer)["/deps"].endswith(",readonly")
    for argv in docker.calls:
        assert {"--read-only", "--cap-drop", "ALL", "no-new-privileges"} <= set(argv)
        assert not any("TOKEN" in a or "KEY" in a or "/workspace" in a for a in argv)
    again = await prep.prepare(req("flask==3.1.0"))
    assert again == bundle and len(docker.calls) == 2  # cache hit: no container started


async def test_the_cache_key_covers_requests_image_and_index(tmp_path: Path) -> None:
    a = preparer(tmp_path, FakeDocker())
    b = DockerDependencyPreparer(
        root=tmp_path,
        image=IMAGE,
        index_url="https://mirror.example/simple",
        runner=FakeDocker(),
        egress=StaticEgress(),
        verifier=PublishedVerifier(),
    )
    assert a.key(req("flask")) != a.key(req("flask==3.1.0")) != b.key(req("flask"))
    assert a.key(req("flask", "click")) == a.key(req("click", "flask"))


async def test_a_corrupted_bundle_is_refused(tmp_path: Path) -> None:
    prep = preparer(tmp_path, FakeDocker())
    bundle = await prep.prepare(req("flask"))
    assert bundle is not None
    wheel = tmp_path / "deps" / bundle.key / "wheels" / bundle.packages[0].filename
    wheel.chmod(0o644)
    wheel.write_bytes(b"TAMPERED")
    downloads = sum("download" in c for c in prep._runner.calls)  # type: ignore[attr-defined]
    rebuilt = await prep.prepare(req("flask"))  # never mounted: quarantined, rebuilt once
    assert rebuilt is not None and rebuilt.key == bundle.key
    assert sum("download" in c for c in prep._runner.calls) == downloads + 1  # type: ignore[attr-defined]
    assert [p.name for p in (tmp_path / "deps").iterdir() if p.name.startswith(".corrupt-")]
    assert (
        tmp_path / "deps" / bundle.key / "wheels" / bundle.packages[0].filename
    ).read_bytes() == b"FLASK"


async def test_a_tampered_lock_manifest_is_refused(tmp_path: Path) -> None:
    prep = preparer(tmp_path, FakeDocker())
    bundle = await prep.prepare(req("flask"))
    assert bundle is not None
    meta = tmp_path / "deps" / bundle.key / "lock.json"
    meta.chmod(0o644)
    data = json.loads(meta.read_text())
    data["packages"][0]["sha256"] = "0" * 64
    meta.write_text(json.dumps(data))
    rebuilt = await prep.prepare(req("flask"))
    assert rebuilt is not None and rebuilt.packages[0].sha256 == bundle.packages[0].sha256


async def test_a_tampered_installed_site_is_detected_and_never_reused(tmp_path: Path) -> None:
    """The sandbox mounts site/, so site/ itself is digest-checked, not only the wheels."""
    docker = FakeDocker()
    prep = preparer(tmp_path, docker)
    bundle = await prep.prepare(req("flask"))
    assert bundle is not None
    init = Path(bundle.site_dir) / "flask" / "__init__.py"
    init.parent.chmod(0o755)
    init.chmod(0o644)
    init.write_text("import os; os.system('curl evil')")
    rebuilt = await prep.prepare(req("flask"))
    assert rebuilt is not None
    assert (Path(rebuilt.site_dir) / "flask" / "__init__.py").read_text() == ""
    assert sum("download" in c for c in docker.calls) == 2


async def test_a_corrupted_rebuild_fails_closed(tmp_path: Path) -> None:
    docker = FakeDocker()
    prep = preparer(tmp_path, docker)
    bundle = await prep.prepare(req("flask"))
    assert bundle is not None
    (tmp_path / "deps" / bundle.key / "lock.json").chmod(0o644)
    (tmp_path / "deps" / bundle.key / "lock.json").write_text("{not json")
    docker.fail = b"ERROR: No matching distribution found for flask"
    with pytest.raises(DependencyPolicyError, match="dependency_wheel_unavailable"):
        await prep.prepare(req("flask"))
    assert not (tmp_path / "deps" / bundle.key).exists()  # nothing half-published


async def test_the_cache_key_changes_with_the_execution_image_identity(tmp_path: Path) -> None:
    first = preparer(tmp_path, FakeDocker())
    a = await first.prepare(req("flask"))
    other = FakeDocker()
    other.image_id = b"sha256:" + b"2" * 64  # same tag, rebuilt image (e.g. another Python)
    b = await preparer(tmp_path, other).prepare(req("flask"))
    assert a is not None and b is not None and a.key != b.key
    assert len(other.calls) == 2  # a fresh download + install, not a reuse


async def test_an_unknown_execution_image_fails_closed(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.image_id = b""
    with pytest.raises(DependencyPolicyError, match="dependency_image_unavailable"):
        await preparer(tmp_path, docker).prepare(req("flask"))
    assert docker.calls == []


async def test_a_resolver_timeout_is_classified(tmp_path: Path) -> None:
    class Slow(FakeDocker):
        async def run(
            self, argv: Sequence[str], *, deadline: float, max_output: int
        ) -> ProcessResult:
            if "download" in argv:
                return ProcessResult(
                    exit_code=-9, stdout=b"", stderr=b"", truncated=False, timed_out=True
                )
            return await super().run(argv, deadline=deadline, max_output=max_output)

    prep = preparer(tmp_path, Slow())
    with pytest.raises(DependencyPolicyError, match="dependency_preparation_timeout"):
        await prep.prepare(req("flask"))
    assert [p for p in (tmp_path / "deps").iterdir() if not p.name.startswith(".")] == []


async def test_installed_size_is_bounded(tmp_path: Path) -> None:
    class Big(FakeDocker):
        async def run(
            self, argv: Sequence[str], *, deadline: float, max_output: int
        ) -> ProcessResult:
            result = await super().run(argv, deadline=deadline, max_output=max_output)
            if "install" in argv:
                site = Path(mounts(list(argv))["/site"])
                (site / "blob.bin").write_bytes(b"x" * 5000)
            return result

    prep = preparer(tmp_path, Big(), max_bytes=1000)
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        await prep.prepare(req("flask"))


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (b"ERROR: No matching distribution found for nativepkg", "dependency_wheel_unavailable"),
        (b"ERROR: ResolutionImpossible: conflicting dependencies", "dependency_conflict"),
        (
            b"ERROR: Package 'x' requires a different Requires-Python",
            "dependency_python_unsupported",
        ),
        (b"network unreachable", "dependency_preparation_failed"),
    ],
)
async def test_resolver_failures_fail_closed(tmp_path: Path, output: bytes, code: str) -> None:
    with pytest.raises(DependencyPolicyError, match=code):
        await preparer(tmp_path, FakeDocker(fail=output)).prepare(req("x"))
    assert not any((tmp_path / "deps").iterdir())  # no partial bundle is left behind


async def test_source_distributions_are_never_accepted(tmp_path: Path) -> None:
    docker = FakeDocker({"flask-3.1.0.tar.gz": b"SDIST"})
    with pytest.raises(DependencyPolicyError, match="dependency_wheel_unavailable"):
        await preparer(tmp_path, docker).prepare(req("flask"))
    assert len(docker.calls) == 1  # nothing was installed


async def test_count_and_size_limits(tmp_path: Path) -> None:
    many = {f"p{i}-1.0-py3-none-any.whl": b"x" for i in range(3)}
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        await preparer(tmp_path, FakeDocker(many), max_packages=2).prepare(req("p0"))
    big = {"big-1.0-py3-none-any.whl": b"x" * 2048}
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        await preparer(tmp_path, FakeDocker(big), max_bytes=1024).prepare(req("big"))


async def test_two_versions_of_one_package_conflict(tmp_path: Path) -> None:
    files = {"a-1.0-py3-none-any.whl": b"1", "a-2.0-py3-none-any.whl": b"2"}
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        await preparer(tmp_path, FakeDocker(files)).prepare(req("a"))


async def test_docker_unavailable(tmp_path: Path) -> None:
    class Broken:
        async def run(self, *a: object, **k: object) -> ProcessResult:
            raise OSError

    prep = DockerDependencyPreparer(
        root=tmp_path,
        image=IMAGE,
        index_url="https://pypi.org/simple",
        runner=Broken(),
        egress=StaticEgress(),
        verifier=PublishedVerifier(),
    )
    with pytest.raises(DependencyPolicyError, match="dependency_preparation_failed"):
        await prep.prepare(req("x"))
    assert "pypi.org" in repr(prep)


async def test_extras_and_declared_hashes_are_verified_and_keyed(tmp_path: Path) -> None:
    wheel_bytes = b"UVICORN_WHEEL"
    good_hash = hashlib.sha256(wheel_bytes).hexdigest()
    docker = FakeDocker({"uvicorn-0.30.0-py3-none-any.whl": wheel_bytes})
    prep = preparer(tmp_path, docker)

    pinned = (DependencyRequest("uvicorn", (">=0.30",), extras=("standard",), hashes=(good_hash,)),)
    other_pin = (
        DependencyRequest("uvicorn", (">=0.30",), extras=("standard",), hashes=("0" * 64,)),
    )
    assert prep.key(pinned) != prep.key(other_pin)

    bundle = await prep.prepare(pinned)
    assert bundle is not None and bundle.packages[0].sha256 == good_hash

    with pytest.raises(DependencyPolicyError, match="dependency_hash_mismatch"):
        await prep.prepare(other_pin)
    assert not (tmp_path / "deps" / prep.key(other_pin)).exists()


async def test_invalid_requirement_stderr_is_classified(tmp_path: Path) -> None:
    docker = FakeDocker(fail=b"ERROR: Invalid requirement: 'bad=='")
    with pytest.raises(DependencyPolicyError, match="dependency_request_invalid"):
        await preparer(tmp_path, docker).prepare(req("x"))
