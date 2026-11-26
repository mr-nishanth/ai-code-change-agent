"""Only a verified bundle under the dependency root is mounted, always read-only, and the sandbox
keeps --network none (ADR-010)."""

import dataclasses
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces.errors import WorkspaceRuntimeError
from ai_code_change_agent.execution.docker.dependency_mount import dependency_args
from ai_code_change_agent.execution.docker.sandbox import SandboxLimits, sandbox_run_argv
from tests.unit.execution.fakes import FILES, ListBaseline, make_runtime, spec


async def test_a_bundle_under_the_root_is_mounted_read_only_for_validation(tmp_path: Path) -> None:
    site = tmp_path / "deps" / "k1" / "site"
    site.mkdir(parents=True)
    runtime, _ = make_runtime(tmp_path, dependency_root=tmp_path / "deps")
    handle = await runtime.create(
        dataclasses.replace(spec(), dependency_dir=str(site)), ListBaseline(FILES)
    )
    assert handle.dependency_dir == str(site.resolve())
    argv = runtime._docker_run_argv(handle, "aica-x-1", "img:1", tmp_path, ["true"], ".", {})
    text = " ".join(argv)
    assert f"src={site.resolve()},dst=/opt/deps,readonly" in text
    assert "--network none" in text and "PYTHONPATH=/opt/deps" in text


@pytest.mark.parametrize("where", ["outside", "missing", "traversal"])
async def test_other_directories_are_refused(tmp_path: Path, where: str) -> None:
    (tmp_path / "deps").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    value = {
        "outside": str(outside),
        "missing": str(tmp_path / "deps" / "nope"),
        "traversal": str(tmp_path / "deps" / ".." / "elsewhere"),
    }[where]
    runtime, _ = make_runtime(tmp_path, dependency_root=tmp_path / "deps")
    with pytest.raises(WorkspaceRuntimeError, match="dependency_dir_not_allowed"):
        await runtime.create(dataclasses.replace(spec(), dependency_dir=value), ListBaseline(FILES))


async def test_without_a_configured_root_no_bundle_can_be_mounted(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    runtime, _ = make_runtime(tmp_path)
    with pytest.raises(WorkspaceRuntimeError, match="dependency_dir_not_allowed"):
        await runtime.create(
            dataclasses.replace(spec(), dependency_dir=str(site)), ListBaseline(FILES)
        )


async def test_the_agent_sandbox_mounts_the_bundle_read_only_and_stays_offline(
    tmp_path: Path,
) -> None:
    site = tmp_path / "deps" / "k" / "site"
    site.mkdir(parents=True)
    runtime, _ = make_runtime(tmp_path, dependency_root=tmp_path / "deps")
    handle = await runtime.create(
        dataclasses.replace(spec(), dependency_dir=str(site)), ListBaseline(FILES)
    )
    argv = sandbox_run_argv(
        docker="docker",
        name="aica-sbx-12345678-0123456789ab",
        image="img:1",
        work=tmp_path,
        handle=handle,
        writable=True,
        limits=SandboxLimits(),
        uid=501,
        gid=20,
    )
    text = " ".join(argv)
    assert "dst=/opt/deps,readonly" in text and "--network none" in text
    assert dependency_args(None) == []
