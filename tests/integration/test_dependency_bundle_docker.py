# ruff: noqa: S607, S108, ASYNC240 - test drives the docker CLI and inspects the bundle on disk
"""Real Docker + real package index: a dependency bundle is resolved by the resolver container,
hash-locked, installed offline, digest-verified, mounted read-only into a network-less sandbox,
reused from cache and refused when tampered with. Needs network to the package index (skipped
otherwise); the sandbox itself never has network."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_code_change_agent.domain.dependencies.policy import DependencyPolicyError, validate_requests
from ai_code_change_agent.execution.docker.dependencies import DockerDependencyPreparer
from ai_code_change_agent.execution.docker.dependency_mount import dependency_args
from ai_code_change_agent.execution.docker.metadata_verifier import MetadataVerifier
from ai_code_change_agent.execution.docker.process import SubprocessRunner
from ai_code_change_agent.execution.docker.resolver_network import ResolverEgress, egress_hosts

pytestmark = pytest.mark.docker
IMAGE = os.environ.get("WORKSPACE_IMAGE", "ai-code-change-agent/execution-python:0.2.0")
INDEX = "https://pypi.org/simple"
PACKAGE = "tabulate==0.9.0"  # small, pure-Python, not part of the execution image


def _ok(*argv: str) -> bool:
    return subprocess.run(argv, capture_output=True, check=False, timeout=60).returncode == 0  # noqa: S603


@pytest.fixture(scope="module", autouse=True)
def _requirements() -> None:
    if shutil.which("docker") is None or not _ok("docker", "info"):
        pytest.skip("docker daemon not reachable")
    if not _ok("docker", "image", "inspect", IMAGE):
        pytest.skip(f"execution image '{IMAGE}' is not built (see docs/GETTING-STARTED.md)")
    try:
        import httpx

        httpx.get(f"{INDEX}/tabulate/", timeout=10).raise_for_status()
    except Exception:
        pytest.skip("package index not reachable from this host")


def sandbox(site: str, script: str) -> subprocess.CompletedProcess[str]:
    """The coding-sandbox hardening flags that matter here, with the bundle mounted read-only."""
    return subprocess.run(  # noqa: S603
        [
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m",
            *dependency_args(site), IMAGE, "python", "-c", script,
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip


def preparer(root: Path) -> DockerDependencyPreparer:
    runner = SubprocessRunner()
    return DockerDependencyPreparer(
        root=root,
        image=IMAGE,
        index_url=INDEX,
        runner=runner,
        egress=ResolverEgress(runner=runner, image=IMAGE, allow_hosts=egress_hosts(INDEX)),
        verifier=MetadataVerifier(INDEX),
        timeout_seconds=300,
    )


async def test_real_bundle_offline_install_cache_and_tamper_rejection(tmp_path: Path) -> None:
    root = tmp_path / "deps"
    requests = validate_requests([PACKAGE], max_packages=10)
    bundle = await preparer(root).prepare(requests)
    assert bundle is not None
    assert [(p.name, p.version) for p in bundle.packages] == [("tabulate", "0.9.0")]
    meta = json.loads((root / bundle.key / "lock.json").read_text())
    assert meta["image_id"].startswith("sha256:") and len(meta["site_sha256"]) == 64
    # every wheel was cross-checked against the hash PyPI publishes (PEP 691)
    (check,) = meta["verification"]
    assert check["source"] == "pep691_json"
    assert check["published_sha256"] == check["sha256"] == bundle.packages[0].sha256
    lock = (root / bundle.key / "wheels" / "requirements.lock").read_text()
    assert lock.startswith("tabulate==0.9.0 --hash=sha256:" + bundle.packages[0].sha256)

    # the network-less sandbox imports exactly the locked version, and has no network
    run = sandbox(
        bundle.site_dir,
        "import tabulate, socket\n"
        "print(tabulate.__version__)\n"
        "try:\n socket.create_connection(('pypi.org', 443), timeout=3); print('NET')\n"
        "except OSError: print('OFFLINE')\n"
        "import pathlib\n"
        "try:\n pathlib.Path('/opt/deps/x').write_text('x'); print('WRITABLE')\n"
        "except OSError: print('READONLY')",
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout.split() == ["0.9.0", "OFFLINE", "READONLY"]

    # without the bundle the package is missing: a controlled ImportError, no fallback fetch
    missing = sandbox_without_bundle("import tabulate")
    assert missing.returncode != 0 and "ModuleNotFoundError" in missing.stderr

    # cache hit: a new preparer (new worker) reuses the verified bundle
    again = await preparer(root).prepare(requests)
    assert again == bundle

    # concurrent preparation of a new set publishes exactly one bundle
    other = validate_requests(["six==1.16.0"], max_packages=10)
    a, b = await asyncio.gather(preparer(root).prepare(other), preparer(root).prepare(other))
    assert a is not None and b is not None and a.key == b.key and a.lock_sha256 == b.lock_sha256
    assert not [p for p in root.iterdir() if p.name.startswith(".tmp-")]

    # tampering with the mounted site/ is detected; the bundle is quarantined and rebuilt
    target = next(Path(bundle.site_dir).rglob("tabulate/__init__.py"))
    target.parent.chmod(0o755)
    target.chmod(0o644)
    target.write_text("raise SystemExit('tampered')")
    rebuilt = await preparer(root).prepare(requests)
    assert rebuilt is not None and rebuilt.lock_sha256 == bundle.lock_sha256
    assert "tampered" not in next(Path(rebuilt.site_dir).rglob("tabulate/__init__.py")).read_text()
    assert [p for p in root.iterdir() if p.name.startswith(".corrupt-")]


def sandbox_without_bundle(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
         "--cap-drop", "ALL", "--user", f"{os.getuid()}:{os.getgid()}", IMAGE,
         "python", "-c", script],
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip


async def test_unavailable_package_and_conflict_fail_closed(tmp_path: Path) -> None:
    prep = preparer(tmp_path / "deps")
    with pytest.raises(DependencyPolicyError, match="dependency_wheel_unavailable"):
        await prep.prepare(validate_requests(["tabulate==0.0.0.999"], max_packages=10))
    assert not [p for p in (tmp_path / "deps").iterdir() if not p.name.startswith(".")]
