"""PostgreSQL: LangGraph checkpoints in their own schema survive a process restart; the operator
CLI records hash-bound approvals and wakes the workflow through the job queue."""

import asyncio
import io
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.cli.main import main
from ai_code_change_agent.core.config import get_settings
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.orchestration.langgraph.checkpointer import postgres_checkpointer
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.ports import GateState
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from tests.integration.conftest import query
from tests.integration.test_jobs_and_changes_db import _attempt, sessions, sql  # noqa: F401
from tests.unit.orchestration.test_workflow import ScriptedSteps, happy

pytestmark = pytest.mark.integration
S = AttemptStatus


async def test_checkpoints_live_in_their_own_schema_and_survive_restart(database_url: str) -> None:
    schema = f"lg_test_{uuid.uuid4().hex[:8]}"
    steps = ScriptedSteps(happy())
    steps.gates["PLAN"] = GateState.PENDING
    attempt = uuid.uuid4()
    async with postgres_checkpointer(database_url, schema) as saver:
        await WorkflowRunner(build_graph(steps, saver)).start(attempt)
    assert steps.calls[-1] == "gate:PLAN"
    tables = {
        r[0]
        for r in query(
            database_url,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        )
    }
    assert {"checkpoints", "checkpoint_writes"} <= tables
    public = {
        r[0]
        for r in query(
            database_url,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
        )
    }
    assert "checkpoints" not in public  # workflow state is separate from domain tables
    steps.gates["PLAN"] = GateState.APPROVED
    async with postgres_checkpointer(database_url, schema) as saver:  # a new process
        runner = WorkflowRunner(build_graph(steps, saver))
        await runner.resume(attempt)
    assert steps.calls.count("prepare") == 1 and steps.calls[-1] == "cleanup"
    sql(database_url, f"DROP SCHEMA {schema} CASCADE")


async def test_the_cli_records_an_approval_and_enqueues_a_resume(
    sessions: async_sessionmaker[AsyncSession],  # noqa: F811 - the imported fixture
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, aid = await _attempt(sessions)
    await svc.transition(aid, S.PREPARING)
    await svc.pin_baseline(aid, "c" * 40)
    await svc.transition(aid, S.PLANNING)
    await svc.record_plan(aid, "art_p", "a" * 64)
    await svc.transition(aid, S.AWAITING_PLAN_APPROVAL)
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        out, err = io.StringIO(), io.StringIO()
        assert (
            await asyncio.to_thread(
                main,
                ["changes", "approve", str(aid), "--gate", "PLAN", "--sha256", "b" * 64],
                out=out,
                err_out=err,
            )
            == 1
        )
        assert "artifact_changed" in err.getvalue()  # a stale hash is refused
        assert (
            await asyncio.to_thread(
                main,
                ["changes", "approve", str(aid), "--gate", "PLAN", "--sha256", "a" * 64],
                out=out,
                err_out=err,
            )
            == 0
        )
        assert (
            await asyncio.to_thread(main, ["changes", "show", str(aid)], out=out, err_out=err) == 0
        )
    finally:
        get_settings.cache_clear()
    assert (
        '"decision": "APPROVED"' in out.getvalue() and '"actor_verified": false' in out.getvalue()
    )
    assert query(database_url, "SELECT kind, status FROM jobs") == [
        ("code_change.resume", "QUEUED")
    ]
