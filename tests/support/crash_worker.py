"""A dependency-preparing worker process that SIGKILLs itself at a chosen stage.

Run as: python -m tests.support.crash_worker <root> <stage> <requirement>
Docker is replaced by a fake runner (the crash is of the worker, which is what owns publication);
the kill is a real SIGKILL of this process, so no Python cleanup (finally blocks) runs.
"""

import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from ai_code_change_agent.domain.dependencies.policy import validate_requests
from ai_code_change_agent.execution.docker import dependencies as deps_mod
from ai_code_change_agent.execution.docker.dependencies import DockerDependencyPreparer
from ai_code_change_agent.execution.docker.process import ProcessResult
from tests.support.dependency_fakes import PublishedVerifier, StaticEgress

IMAGE = "ai-code-change-agent/execution-python:0.2.0"
WHEEL = b"W" * 4096


def die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(60)  # SIGKILL is delivered asynchronously: never run past the crash point


def mounts(argv: Sequence[str]) -> dict[str, str]:
    out = {}
    for i, a in enumerate(argv):
        if a == "--mount":
            parts = dict(p.split("=", 1) for p in argv[i + 1].split(",") if "=" in p)
            out[parts["dst"]] = parts["src"]
    return out


class Runner:
    def __init__(self, stage: str, wheel: str) -> None:
        self.stage, self.wheel = stage, wheel
        self.calls = 0

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        argv = list(argv)
        ok = ProcessResult(exit_code=0, stdout=b"", stderr=b"", truncated=False, timed_out=False)
        if argv[1:3] == ["image", "inspect"]:
            return ProcessResult(
                exit_code=0, stdout=b"sha256:" + b"1" * 64, stderr=b"", truncated=False,
                timed_out=False,
            )  # fmt: skip
        self.calls += 1
        m = mounts(argv)
        if "download" in argv:
            if self.stage == "before_download":
                die()
            target = Path(m["/wheels"]) / self.wheel
            if self.stage == "mid_download":
                target.write_bytes(WHEEL[:1000])  # a truncated wheel on disk
                die()
            target.write_bytes(WHEEL)
        else:
            site = Path(m["/site"])
            (site / "pkg").mkdir()
            (site / "pkg" / "__init__.py").write_text("x = 1\n")
            if self.stage == "mid_install":
                (site / "pkg" / "half.py").write_text("partial")
                die()
        return ok


def preparer(root: Path, stage: str, wheel: str) -> DockerDependencyPreparer:
    verifier = PublishedVerifier()
    if stage == "mid_verify":
        verifier.verify = lambda packages: die()  # type: ignore[method-assign,assignment,return-value]
    return DockerDependencyPreparer(
        root=root, image=IMAGE, index_url="https://pypi.org/simple", runner=Runner(stage, wheel),
        egress=StaticEgress(), verifier=verifier, uid=os.getuid(), gid=os.getgid(),
    )  # fmt: skip


def install_hooks(stage: str, root: Path) -> None:
    real_freeze, real_rename = deps_mod._freeze, Path.rename
    if stage == "before_publish":

        def freeze_then_die(path: Path) -> None:
            real_freeze(path)
            die()

        deps_mod._freeze = freeze_then_die  # type: ignore[assignment]
    if stage in ("mid_publish", "after_publish"):

        def rename(self: Path, target: str | os.PathLike[str]) -> Path:
            if self.parent == root and self.name.startswith(".tmp-"):
                if stage == "mid_publish":
                    die()
                out = real_rename(self, target)
                die()
                return out
            return real_rename(self, target)

        Path.rename = rename  # type: ignore[method-assign,assignment]


async def main(root: Path, stage: str, requirement: str) -> None:
    name, version = requirement.split("==")
    wheel = f"{name}-{version}-py3-none-any.whl"
    install_hooks(stage, root)
    await preparer(root, stage, wheel).prepare(validate_requests([requirement], max_packages=5))
    if stage == "before_record":  # bundle ready, workflow never recorded the attempt step
        die()
    print("prepared", flush=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
