"""Crash recovery: a re-run of the same attempt replaces the workspace its dead run left."""

import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.workspaces.errors import WorkspaceExistsError
from tests.unit.execution.fakes import ListBaseline, make_runtime, spec


async def test_a_rerun_of_the_same_attempt_destroys_the_stale_workspace(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    attempt = uuid.uuid4()
    first = await runtime.create(spec(attempt), ListBaseline({"a.txt": b"a"}))
    with pytest.raises(WorkspaceExistsError):
        await runtime.create(spec(attempt), ListBaseline({"a.txt": b"a"}))
    assert await runtime.destroy_attempt_workspaces(uuid.uuid4()) == 0  # another attempt: untouched
    assert await runtime.destroy_attempt_workspaces(attempt) == 1
    second = await runtime.create(spec(attempt), ListBaseline({"a.txt": b"a"}))
    assert second.id != first.id
