"""Real Docker: the sandbox is probed from inside a container started by the runtime.

Run with: uv run pytest -m docker -rs   (needs Docker and the local execution image, see
docs/SETUP-LOCAL.md). Missing Docker or image is a SKIP with a reason, never a pass.
The probes call the runtime's internal `_execute` (the policy gate only allows test/lint commands,
not introspection); real validation commands go through `run_command`.
"""

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces.models import (
    ChangeLimits,
    ChangeOp,
    CommandStatus,
    FileChange,
    WorkspaceHandle,
    WorkspaceSpec,
)
from ai_code_change_agent.application.workspaces.policy import authorise_command
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.execution.baseline import DirectoryBaseline
from ai_code_change_agent.execution.docker.runtime import (
    ContainerLimits,
    DockerWorkspaceRuntime,
)

pytestmark = pytest.mark.docker
IMAGE = os.environ.get("WORKSPACE_IMAGE", "ai-code-change-agent/execution-python:0.2.0")
MEMORY_MB, PIDS = 256, 64


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def docker_ready() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    if _docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        pytest.skip("docker daemon not reachable")
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.skip(f"execution image '{IMAGE}' is not built (see docs/SETUP-LOCAL.md)")
    if os.getuid() == 0:
        pytest.skip("must not run as root")


def leftovers() -> list[str]:
    return _docker("ps", "-aq", "--filter", "label=aica.managed=true").stdout.split()


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    src = tmp_path / "sample"
    (src / "app").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "app" / "greeting.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n')
    (src / "tests" / "test_greeting.py").write_text(
        "import sys\nsys.path.insert(0, '.')\nfrom app.greeting import greet\n\n\n"
        "def test_greet():\n    assert greet('Ada') == 'Hello, Ada!'\n"
    )
    return src


@pytest.fixture
async def rt(tmp_path: Path) -> AsyncIterator[DockerWorkspaceRuntime]:
    root = tmp_path / "workspaces"
    yield DockerWorkspaceRuntime(
        root=root,
        images={"python": IMAGE},
        policy=CommandPolicy(),
        limits=ContainerLimits(memory_mb=MEMORY_MB, pids=PIDS, max_workspace_bytes=2 * 1024 * 1024),
    )
    assert leftovers() == [], "a test left a container behind"


async def make(rt: DockerWorkspaceRuntime, sample: Path) -> WorkspaceHandle:
    spec = WorkspaceSpec(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "d" * 40)
    return await rt.create(spec, DirectoryBaseline(sample))


async def probe(
    rt: DockerWorkspaceRuntime, h: WorkspaceHandle, code: str, deadline: int = 30
) -> str:
    result = await rt._execute(
        h, command_id="probe", image=IMAGE, argv=["python", "-c", code], cwd=".",
        deadline_seconds=deadline, env={},
    )  # fmt: skip
    assert result.status is CommandStatus.PASSED, (
        result.status,
        result.reason,
        result.stderr[-300:],
    )
    return result.stdout.decode().strip()


async def test_container_has_no_network(rt: DockerWorkspaceRuntime, sample: Path) -> None:
    h = await make(rt, sample)
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), 3)\n"
        "    print('CONNECTED')\n"
        "except OSError:\n"
        "    print('blocked')\n"
        "print(len(open('/proc/net/route').read().splitlines()) - 1)\n"  # routes besides the header\n
    )
    out = (await probe(rt, h, code)).splitlines()
    assert out[0] == "blocked" and out[1] == "0"  # and the routing table is empty: no way out
    await rt.destroy(h)


async def test_container_runs_unprivileged_with_a_read_only_root(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    code = (
        "import os\n"
        "print(os.getuid(), os.getgid())\n"
        "def can_write(p):\n"
        "    try:\n"
        "        open(p, 'w').write('x'); return True\n"
        "    except OSError:\n"
        "        return False\n"
        "print(can_write('/etc/pwned'), can_write('/usr/pwned'), can_write('/workspace/ok.txt'), can_write('/tmp/ok'))\n"
        "status = dict(l.split(':\\t', 1) for l in open('/proc/self/status').read().splitlines() if ':\\t' in l)\n"
        "print(status['CapEff'].strip(), status['CapBnd'].strip(), status['NoNewPrivs'].strip())\n"
    )
    uid_gid, writes, caps = (await probe(rt, h, code)).splitlines()
    assert uid_gid == f"{os.getuid()} {os.getgid()}" and os.getuid() != 0
    assert writes == "False False True True"  # root fs read-only; /workspace and /tmp writable
    assert caps == "0000000000000000 0000000000000000 1"  # no capabilities, no_new_privs set
    assert (Path(rt._root) / str(h.attempt_id) / h.id / "work" / "ok.txt").exists()
    await rt.destroy(h)


async def test_container_cannot_see_the_host_or_secrets(
    rt: DockerWorkspaceRuntime, sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://leak:leak@h/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "LEAKED-AWS")
    monkeypatch.setenv("GITHUB_TOKEN", "LEAKED-GH")
    h = await make(rt, sample)
    code = (
        "import os\n"
        "print(sorted(k for k in os.environ if k.startswith(('DATABASE','AWS','GITHUB','JIRA','ANTHROPIC','LLM'))))\n"
        "print(any('LEAKED' in v for v in os.environ.values()))\n"
        "print([p for p in ('/Users','/var/run/docker.sock','/run/docker.sock','/host') if os.path.exists(p)])\n"
        "print(os.listdir('/home'))\n"
        "print(sorted(os.listdir('/workspace')))\n"
        "print(open('/proc/self/mountinfo').read().count('docker.sock'))\n"
    )
    out = (await probe(rt, h, code)).splitlines()
    assert out[0] == "[]" and out[1] == "False"
    assert out[2] == "[]"  # no host home, no docker socket
    assert out[3] == "[]"  # /home is the image's own empty directory, not the host's
    assert out[4] == "['app', 'tests']"  # only this workspace
    assert out[5] == "0"
    await rt.destroy(h)


async def test_resource_limits_are_applied(rt: DockerWorkspaceRuntime, sample: Path) -> None:
    h = await make(rt, sample)
    code = (
        "print(open('/sys/fs/cgroup/memory.max').read().strip())\n"
        "print(open('/sys/fs/cgroup/pids.max').read().strip())\n"
    )
    mem, pids = (await probe(rt, h, code)).splitlines()
    assert int(mem) == MEMORY_MB * 1024 * 1024 and int(pids) == PIDS
    await rt.destroy(h)


async def test_fork_bomb_is_contained_by_the_pids_limit(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    code = (
        "import os\nn=0\n"
        "try:\n"
        "    for _ in range(500):\n"
        "        if os.fork() == 0:\n"
        "            import time; time.sleep(5); os._exit(0)\n"
        "        n += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print('contained' if n < 200 else 'UNCONTAINED')\n"
    )
    assert await probe(rt, h, code, deadline=60) == "contained"
    await rt.destroy(h)


async def test_timeout_kills_the_container(rt: DockerWorkspaceRuntime, sample: Path) -> None:
    h = await make(rt, sample)
    started = time.monotonic()
    result = await rt._execute(
        h, command_id="sleep", image=IMAGE, argv=["python", "-c", "import time; time.sleep(120)"],
        cwd=".", deadline_seconds=4, env={},
    )  # fmt: skip
    assert result.status is CommandStatus.TIMED_OUT and result.reason == "command_timeout"
    assert time.monotonic() - started < 25
    assert leftovers() == []  # killed and removed, not left running
    await rt.destroy(h)


async def test_cancellation_kills_the_container(rt: DockerWorkspaceRuntime, sample: Path) -> None:
    h = await make(rt, sample)
    task = asyncio.create_task(
        rt._execute(
            h,
            command_id="sleep",
            image=IMAGE,
            argv=["python", "-c", "import time; time.sleep(120)"],
            cwd=".",
            deadline_seconds=100,
            env={},
        )
    )
    for _ in range(100):  # wait until the container is really running
        if leftovers():
            break
        await asyncio.sleep(0.1)
    assert leftovers(), "container never started"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert leftovers() == []
    await rt.destroy(h)


async def test_output_is_capped_and_marked_truncated(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    result = await rt._execute(
        h, command_id="noisy", image=IMAGE, argv=["python", "-c", "print('x' * 2_000_000)"],
        cwd=".", deadline_seconds=30, env={},
    )  # fmt: skip
    assert result.status is CommandStatus.PASSED and result.output_truncated
    assert len(result.stdout) <= 262_144
    await rt.destroy(h)


async def test_disk_quota_is_enforced_after_a_command(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    result = await rt._execute(
        h, command_id="fill", image=IMAGE,
        argv=["python", "-c", "open('/workspace/big.bin','wb').write(b'0'*(5*1024*1024))"],
        cwd=".", deadline_seconds=30, env={},
    )  # fmt: skip
    assert (result.status, result.reason) == (CommandStatus.ERROR, "workspace_size_exceeded")
    await rt.destroy(h)


async def test_attempts_cannot_see_each_others_workspaces(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    a, b = await make(rt, sample), await make(rt, sample)
    await rt.apply_changes(
        b, [FileChange("only_in_b.txt", ChangeOp.CREATE, b"b\n")], ChangeLimits()
    )
    code = f"import os\nprint(os.path.exists('/workspace/only_in_b.txt'), os.path.exists({str(rt._root)!r}))\n"
    assert (
        await probe(rt, a, code) == "False False"
    )  # B's file is absent and host paths do not exist
    await rt.destroy(a)
    await rt.destroy(b)


PROFILE = RepositoryProfile.model_validate(
    {
        "schema_version": 1, "repository_key": "demo", "toolchains": ["python"],
        "command_policy": "default-v1", "validation_policy": "standard",
        "commands": [
            {"id": "tests", "kind": "test", "toolchain": "python", "argv": ["python", "-m", "pytest", "-q", "tests"], "timeout_seconds": 120},
            {"id": "lint", "kind": "lint", "toolchain": "python", "argv": ["ruff", "check", "app"], "timeout_seconds": 60},
            {"id": "fmt", "kind": "format_check", "toolchain": "python", "argv": ["ruff", "format", "--check", "app"], "timeout_seconds": 60},
            {"id": "types", "kind": "lint", "toolchain": "python", "argv": ["mypy", "app"], "timeout_seconds": 120},
            {"id": "install", "kind": "install", "toolchain": "python", "argv": ["pip", "install", "x"], "network": "package_registry"},
        ],
    }
)  # fmt: skip


async def test_profile_commands_run_in_the_container_and_see_host_side_edits(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    tests = authorise_command(PROFILE, CommandPolicy(), "tests")
    first = await rt.run_command(h, tests)
    assert first.status is CommandStatus.PASSED, first.stdout + first.stderr
    assert b"1 passed" in first.stdout
    for cid in ("lint", "fmt", "types"):  # the image really contains these tools
        res = await rt.run_command(h, authorise_command(PROFILE, CommandPolicy(), cid))
        assert res.status is CommandStatus.PASSED, (cid, res.stdout, res.stderr)
    await rt.apply_changes(
        h,
        [
            FileChange(
                "app/greeting.py", ChangeOp.MODIFY, b'def greet(name):\n    return "broken"\n'
            )
        ],
        ChangeLimits(),
    )
    broken = await rt.run_command(h, tests)
    assert broken.status is CommandStatus.FAILED and broken.exit_code == 1  # a real failure
    await rt.reset_work(h)
    assert (await rt.run_command(h, tests)).status is CommandStatus.PASSED  # baseline restored
    await rt.destroy(h)


async def test_network_commands_never_reach_docker(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    from ai_code_change_agent.application.workspaces.errors import CommandBlockedError

    with pytest.raises(CommandBlockedError, match="network_commands_unsupported"):
        authorise_command(PROFILE, CommandPolicy(), "install")
    assert leftovers() == []


async def test_destroy_removes_files_and_containers(
    rt: DockerWorkspaceRuntime, sample: Path
) -> None:
    h = await make(rt, sample)
    workspace_dir = Path(rt._root) / str(h.attempt_id) / h.id
    assert workspace_dir.is_dir()
    result = await rt.destroy(h)
    assert result.clean and not workspace_dir.exists() and leftovers() == []
    assert (await rt.destroy(h)).clean  # idempotent


async def test_check_ready_sees_the_image(rt: DockerWorkspaceRuntime) -> None:
    assert await rt.check_ready() == {"docker": True, "image:python": True}
