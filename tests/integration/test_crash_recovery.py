# ruff: noqa: ASYNC240 - the test inspects the bundle directory on disk
"""Real worker crashes during dependency preparation (ADR-010).

Each scenario starts a separate worker process (tests/support/crash_worker.py) that is killed
with a real SIGKILL at one stage of preparation, so no cleanup code runs. A fresh worker (this
process) must then recover: nothing partial is ever a cache entry, a previously valid bundle is
untouched, the recovered bundle verifies, and a fully published bundle is reused, not rebuilt.
"""

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from ai_code_change_agent.domain.dependencies.policy import validate_requests
from tests.support import crash_worker

REPO = Path(__file__).resolve().parents[2]
REQ = "crashpkg==1.0"
OTHER = "steadypkg==2.0"
PARTIAL = [
    "before_download", "mid_download", "mid_verify", "mid_install", "before_publish",
    "mid_publish",
]  # fmt: skip
PUBLISHED = ["after_publish", "before_record"]


def run_worker(root: Path, stage: str, requirement: str = REQ) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tests.support.crash_worker", str(root), stage, requirement],
        cwd=REPO, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def entries(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


async def recover(root: Path, requirement: str = REQ) -> tuple[object, int]:
    prep = crash_worker.preparer(root, "none", requirement.replace("==", "-") + "-py3-none-any.whl")
    runner: crash_worker.Runner = prep._runner  # type: ignore[assignment]
    bundle = await prep.prepare(validate_requests([requirement], max_packages=5))
    return bundle, runner.calls


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "deps"
    done = run_worker(r, "none", OTHER)  # a valid bundle for another set already exists
    assert done.returncode == 0, done.stderr[-2000:]
    return r


@pytest.mark.parametrize("stage", PARTIAL)
async def test_a_crash_before_publication_leaves_nothing_usable(root: Path, stage: str) -> None:
    before = entries(root)
    crashed = run_worker(root, stage)
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr[-2000:]
    assert entries(root) == before  # no new cache entry, partial work only in .tmp-*
    if stage != "before_download":
        assert [p for p in root.iterdir() if p.name.startswith(".tmp-")]

    bundle, docker_calls = await recover(root)
    assert bundle is not None and docker_calls == 2  # rebuilt: resolver + installer
    site = Path(bundle.site_dir)  # type: ignore[attr-defined]
    assert not (site / "pkg" / "half.py").exists()
    assert len(entries(root)) == len(before) + 1

    _, other_calls = await recover(root, OTHER)  # the earlier bundle is intact and reused
    assert other_calls == 0


@pytest.mark.parametrize("stage", PUBLISHED)
async def test_a_crash_after_publication_is_reused_after_verification(
    root: Path, stage: str
) -> None:
    crashed = run_worker(root, stage)
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr[-2000:]
    bundle, docker_calls = await recover(root)
    assert bundle is not None and docker_calls == 0  # verified cache hit, no rebuild
    assert not [p for p in root.iterdir() if p.name.startswith(".tmp-")]


async def test_a_crashed_partial_bundle_never_verifies_as_a_cache_entry(root: Path) -> None:
    """Even if an operator renamed the dead worker's .tmp dir into place, it is refused."""
    run_worker(root, "before_publish")
    (tmp,) = [p for p in root.iterdir() if p.name.startswith(".tmp-")]
    bundle, _ = await recover(root)
    key = bundle.key  # type: ignore[attr-defined]
    for p in [root / key, *(root / key).rglob("*")]:
        if p.is_dir():
            p.chmod(0o755)
    (root / key / "site" / "pkg" / "__init__.py").chmod(0o644)
    (root / key / "site" / "pkg" / "__init__.py").write_text("tampered")
    again, calls = await recover(root)
    assert calls == 2 and again.key == key  # type: ignore[attr-defined]
    assert [p for p in root.iterdir() if p.name.startswith(".corrupt-")]
    assert tmp.exists()  # the dead worker's frozen build dir is never adopted


async def test_two_worker_processes_prepare_the_same_set_concurrently(tmp_path: Path) -> None:
    root = tmp_path / "deps"
    procs = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.crash_worker",
            str(root),
            "none",
            REQ,
            cwd=REPO,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(4)
    ]
    results = await asyncio.gather(*(p.communicate() for p in procs))
    assert all(p.returncode == 0 for p in procs), [r[1][-500:] for r in results]
    assert len(entries(root)) == 1
    assert not [p for p in root.iterdir() if p.name.startswith(".tmp-")]
    _, calls = await recover(root)
    assert calls == 0
