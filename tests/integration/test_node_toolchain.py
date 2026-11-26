"""Real Docker: the node toolchain's execution image, used by the agent sandbox and by the
independent validation runner, offline. Skipped (with a reason) when the image is not built."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.domain.repositories.templates import standard_profile
from ai_code_change_agent.execution.docker.process import SubprocessRunner
from ai_code_change_agent.execution.docker.runtime import DockerWorkspaceRuntime
from ai_code_change_agent.execution.docker.sandbox import SandboxLimits, SandboxManager
from ai_code_change_agent.validation.test_runner import run_commands
from tests.unit.execution.fakes import ListBaseline, spec

pytestmark = pytest.mark.docker
PY = os.environ.get("WORKSPACE_IMAGE", "ai-code-change-agent/execution-python:0.2.0")
NODE = os.environ.get("NODE_WORKSPACE_IMAGE", "ai-code-change-agent/execution-node:0.1.0")
FILES = {
    "package.json": b'{"name":"calc","private":true,"scripts":{"test":"node --test"}}\n',
    "src/add.js": b"module.exports = (a, b) => a + b;\n",
    "test/add.test.js": (
        b"const test = require('node:test');\nconst assert = require('node:assert');\n"
        b"const add = require('../src/add.js');\ntest('adds', () => assert.strictEqual(add(2, 3), 5));\n"
    ),
}


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker argv in a test helper
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def ready() -> None:
    if shutil.which("docker") is None or _docker("version").returncode != 0:
        pytest.skip("docker daemon not reachable")
    for image in (PY, NODE):
        if _docker("image", "inspect", image).returncode != 0:
            pytest.skip(f"execution image '{image}' is not built")


async def test_node_profile_runs_offline_in_the_sandbox_and_in_validation(tmp_path: Path) -> None:
    images = {"python": PY, "node": NODE}
    runtime = DockerWorkspaceRuntime(
        root=tmp_path.resolve() / "ws",  # noqa: ASYNC240 - test setup
        images=images,
        policy=CommandPolicy(),
    )
    manager = SandboxManager(
        image=PY, images=images, runner=SubprocessRunner(),
        limits=SandboxLimits(memory_mb=512, pids=128, command_timeout_seconds=60,
                             disk_bytes=20 * 1024 * 1024, max_output_bytes=20_000, lifetime_seconds=300),
    )  # fmt: skip
    handle = await runtime.create(spec(), ListBaseline(FILES))
    try:
        sandbox = await manager.start(
            handle, runtime.tree_path(handle, "work"), writable=True, toolchain="node"
        )
        try:
            assert sandbox.execute("node -v").output.startswith("v22")
            run = sandbox.execute("npm test")
            assert run.exit_code == 0, run.output
            net = sandbox.execute(
                "node -e \"require('https').get('https://registry.npmjs.org',()=>{}).on('error',()=>process.exit(3))\""
            )
            assert net.exit_code != 0  # no network in the sandbox
            assert sandbox.execute("id -u").output.strip() != "0"
        finally:
            await manager.stop(sandbox)
        profile = RepositoryProfile.model_validate(standard_profile("calc", "node"))
        records = await run_commands(runtime, handle, profile, CommandPolicy())
        assert [(r.command_id, r.status) for r in records] == [("test", "PASSED")], records
    finally:
        await runtime.destroy(handle)
    assert _docker("ps", "-aq", "--filter", "label=aica.role=agent-sandbox").stdout.split() == []
