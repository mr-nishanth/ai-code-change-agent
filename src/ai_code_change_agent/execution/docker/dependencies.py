"""Trusted dependency preparation (ADR-010): wheels only, hash-locked, installed offline.

Runs in the worker (trusted control plane), never in the coding sandbox:

1. a short-lived *resolver* container runs one fixed command, `pip download --only-binary=:all:`
   against the single configured index, for the validated requests only. It holds no
   application credentials, no workspace and no agent output, and has a read-only root
   filesystem, all capabilities dropped and a non-root user. Its network egress is limited by
   argv (`--index-url` only); a host firewall is the operator's further control;
2. the worker refuses anything that is not a wheel, enforces count and size limits, hashes every
   file and writes `requirements.lock` (`name==version --hash=sha256:...`);
3. an *installer* container with `--network none` runs
   `pip install --no-index --require-hashes --no-deps --target` from that lock. Installing a wheel
   executes no package code;
4. the bundle is made read-only and moved into place atomically under a content key, which is also
   the cache: a later request with the same key reuses it after re-verifying every hash.

Integrity covers what the sandbox actually mounts: lock.json records the sha256 of every wheel
AND a digest of every file in the installed `site/` tree. The cache key includes the execution
image's immutable id (so a re-tagged image with another Python never reuses an old bundle). A
bundle that fails verification is moved aside (`.corrupt-*`) and rebuilt once from the index;
it is never mounted.
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from ai_code_change_agent.application.dependencies.ports import DependencyBundle, LockedPackage
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.dependencies.policy import (
    DependencyPolicyError,
    DependencyRequest,
    wheel_identity,
)
from ai_code_change_agent.execution.docker.process import ProcessRunner

_log = get_logger("dependencies")
_OUTPUT_CAP = 262_144


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> tuple[str, int]:
    """sha256 over (relative path, kind, mode bits, content hash) of every entry under root, and
    the total file size. Symlinks are refused: a bundle never contains links."""
    digest = hashlib.sha256()
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise DependencyPolicyError("dependency_bundle_corrupted")
        if path.is_dir():
            digest.update(f"d {rel}\n".encode())
            continue
        total += path.stat().st_size
        executable = "x" if path.stat().st_mode & stat.S_IXUSR else "-"
        digest.update(f"f {rel} {executable} {_sha256(path)}\n".encode())
    return digest.hexdigest(), total


def _freeze(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        mode = path.stat().st_mode
        executable = path.is_dir() or bool(mode & stat.S_IXUSR)
        path.chmod(0o555 if executable else 0o444)
    root.chmod(0o555)


class DockerDependencyPreparer:
    def __init__(
        self,
        *,
        root: Path,
        image: str,
        index_url: str,
        runner: ProcessRunner,
        docker: str = "docker",
        max_packages: int = 60,
        max_bytes: int = 200 * 1024 * 1024,
        timeout_seconds: float = 600.0,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        self._root = root
        self._image = image
        self._index = index_url
        self._runner = runner
        self._docker = docker
        self._max_packages = max_packages
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._locks: dict[str, asyncio.Lock] = {}
        self._image_id: str | None = None

    def __repr__(self) -> str:
        return f"DockerDependencyPreparer(index={self._index!r})"

    async def _resolve_image_id(self) -> str:
        """The immutable id of the execution image (Python version and platform included)."""
        if self._image_id is None:
            try:
                result = await self._runner.run(
                    [self._docker, "image", "inspect", "--format", "{{.Id}}", self._image],
                    deadline=30.0,
                    max_output=4096,
                )
            except OSError:  # Docker itself is unavailable
                raise DependencyPolicyError("dependency_preparation_failed") from None
            image_id = (result.stdout or b"").decode(errors="replace").strip()
            if result.exit_code != 0 or not image_id.startswith("sha256:"):
                raise DependencyPolicyError("dependency_image_unavailable")
            self._image_id = image_id
        return self._image_id

    def key(self, requests: tuple[DependencyRequest, ...]) -> str:
        material = {
            "image": self._image,
            "image_id": self._image_id,
            "index": self._index,
            "requests": [r.requirement() for r in requests],
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    async def prepare(self, requests: tuple[DependencyRequest, ...]) -> DependencyBundle | None:
        if not requests:
            return None
        await self._resolve_image_id()
        key = self.key(requests)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            final = self._root / key
            if final.is_dir():
                try:
                    bundle = await asyncio.to_thread(self._verify, final, key)
                except DependencyPolicyError:
                    await asyncio.to_thread(self._quarantine, final, key)
                else:
                    _log.info("dependency_bundle_reused", key=key, packages=len(bundle.packages))
                    return bundle
            return await self._build(key, requests, final)

    def _quarantine(self, final: Path, key: str) -> None:
        """Move a bundle that failed verification out of the cache (kept for inspection)."""
        target = self._root / f".corrupt-{key[:16]}-{uuid.uuid4().hex[:8]}"
        final.chmod(0o700)
        final.rename(target)
        _log.warning("dependency_bundle_quarantined", key=key, moved_to=target.name)

    async def _build(
        self, key: str, requests: tuple[DependencyRequest, ...], final: Path
    ) -> DependencyBundle:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self._root / f".tmp-{key[:16]}-{uuid.uuid4().hex[:8]}"
        try:
            for sub in ("req", "wheels", "site"):
                (tmp / sub).mkdir(mode=0o700, parents=True)
            text = "".join(f"{r.requirement()}\n" for r in requests)
            (tmp / "req" / "requirements.in").write_text(text)
            await self._download(tmp)
            packages = await asyncio.to_thread(self._lock_wheels, tmp / "wheels")
            lock_text = "".join(
                f"{p.name}=={p.version} --hash=sha256:{p.sha256}\n" for p in packages
            )
            (tmp / "wheels" / "requirements.lock").write_text(lock_text)
            await self._install(tmp)
            site_sha, site_bytes = await asyncio.to_thread(_tree_digest, tmp / "site")
            if site_bytes > self._max_bytes * 4:  # wheels expand; bound the installed size too
                raise DependencyPolicyError("dependency_limit_exceeded")
            lock_sha = hashlib.sha256(lock_text.encode()).hexdigest()
            meta = {
                "key": key,
                "image": self._image,
                "index": self._index,
                "requests": [r.requirement() for r in requests],
                "lock_sha256": lock_sha,
                "site_sha256": site_sha,
                "image_id": self._image_id,
                "packages": [p.__dict__ for p in packages],
            }
            (tmp / "lock.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
            await asyncio.to_thread(_freeze, tmp)
            try:
                await asyncio.to_thread(tmp.rename, final)
            except OSError:  # another worker finished first: keep theirs
                if not await asyncio.to_thread(final.is_dir):
                    raise
            _log.info("dependency_bundle_prepared", key=key, packages=len(packages))
            return await asyncio.to_thread(self._verify, final, key)
        finally:
            if tmp.exists():
                await asyncio.to_thread(_remove, tmp)

    def _container(
        self, name: str, *, network: bool, mounts: list[str], argv: list[str]
    ) -> list[str]:
        cli = [
            self._docker, "run", "--rm", "--pull", "never", "--name", name,
            "--label", "aica.managed=true", "--label", f"aica.role={name.split('-')[1]}",
            "--network", "bridge" if network else "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "256", "--memory", "1024m", "--memory-swap", "1024m",
            "--user", f"{self._uid}:{self._gid}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",  # noqa: S108 - inside the container
            "--env", "HOME=/tmp", "--env", "PIP_NO_CACHE_DIR=1",
            "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
        ]  # fmt: skip
        for mount in mounts:
            cli += ["--mount", mount]
        return [*cli, self._image, *argv]

    async def _run(self, argv: list[str], failure: str) -> bytes:
        try:
            result = await self._runner.run(argv, deadline=self._timeout, max_output=_OUTPUT_CAP)
        except OSError:
            raise DependencyPolicyError("dependency_preparation_failed") from None
        if result.timed_out:
            raise DependencyPolicyError("dependency_preparation_timeout")
        if result.exit_code != 0:
            out = (result.stdout or b"") + (result.stderr or b"")
            if b"No matching distribution" in out or b"Could not find a version" in out:
                raise DependencyPolicyError("dependency_wheel_unavailable")
            if b"ResolutionImpossible" in out or b"conflicting dependencies" in out:
                raise DependencyPolicyError("dependency_conflict")
            if b"Requires-Python" in out:
                raise DependencyPolicyError("dependency_python_unsupported")
            raise DependencyPolicyError(failure)
        return result.stdout or b""

    async def _download(self, tmp: Path) -> None:
        name = f"aica-resolver-{uuid.uuid4().hex[:10]}"
        argv = self._container(
            name,
            network=True,
            mounts=[
                f"type=bind,src={tmp / 'req'},dst=/req,readonly",
                f"type=bind,src={tmp / 'wheels'},dst=/wheels",
            ],
            argv=[
                "python", "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir",
                "--index-url", self._index, "--dest", "/wheels", "-r", "/req/requirements.in",
            ],
        )  # fmt: skip
        await self._run(argv, "dependency_preparation_failed")

    async def _install(self, tmp: Path) -> None:
        name = f"aica-installer-{uuid.uuid4().hex[:10]}"
        argv = self._container(
            name,
            network=False,
            mounts=[
                f"type=bind,src={tmp / 'wheels'},dst=/deps,readonly",
                f"type=bind,src={tmp / 'site'},dst=/site",
            ],
            argv=[
                "python", "-m", "pip", "install", "--no-index", "--find-links", "/deps",
                "--require-hashes", "--no-deps", "--no-compile", "--target", "/site",
                "-r", "/deps/requirements.lock",
            ],
        )  # fmt: skip
        await self._run(argv, "dependency_install_failed")

    def _lock_wheels(self, wheels: Path) -> tuple[LockedPackage, ...]:
        files = sorted(p for p in wheels.iterdir())
        if not files:
            raise DependencyPolicyError("dependency_preparation_failed")
        if len(files) > self._max_packages:
            raise DependencyPolicyError("dependency_limit_exceeded")
        total = 0
        packages: list[LockedPackage] = []
        for path in files:
            if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
                raise DependencyPolicyError("dependency_wheel_unavailable")
            name, version = wheel_identity(path.name)
            size = path.stat().st_size
            total += size
            if total > self._max_bytes:
                raise DependencyPolicyError("dependency_limit_exceeded")
            packages.append(LockedPackage(name, version, path.name, _sha256(path), size))
        if len({p.name for p in packages}) != len(packages):
            raise DependencyPolicyError("dependency_conflict")
        return tuple(packages)

    def _verify(self, final: Path, key: str) -> DependencyBundle:
        try:
            meta = json.loads((final / "lock.json").read_text())
            packages = tuple(LockedPackage(**p) for p in meta["packages"])
            lock_text = (final / "wheels" / "requirements.lock").read_bytes()
        except (OSError, ValueError, KeyError, TypeError):
            raise DependencyPolicyError("dependency_bundle_corrupted") from None
        if meta.get("key") != key or hashlib.sha256(lock_text).hexdigest() != meta.get(
            "lock_sha256"
        ):
            raise DependencyPolicyError("dependency_bundle_corrupted")
        present = {p.name for p in (final / "wheels").iterdir() if p.suffix == ".whl"}
        if present != {p.filename for p in packages}:
            raise DependencyPolicyError("dependency_bundle_corrupted")
        for p in packages:
            if _sha256(final / "wheels" / p.filename) != p.sha256:
                raise DependencyPolicyError("dependency_bundle_corrupted")
        site = final / "site"
        if not site.is_dir() or meta.get("site_sha256") != _tree_digest(site)[0]:
            raise DependencyPolicyError("dependency_bundle_corrupted")
        return DependencyBundle(key, str(meta["lock_sha256"]), str(site), packages)


def _remove(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        if p.is_dir() and not p.is_symlink():
            p.chmod(0o700)
    shutil.rmtree(path, ignore_errors=True)
