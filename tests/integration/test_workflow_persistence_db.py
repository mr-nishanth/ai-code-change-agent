"""PostgreSQL: LangGraph checkpoints in their own schema survive a process restart. (The operator
CLI's approval and resume test moved to `tests/integration/floci/test_cli_dynamo.py`: the CLI runs on
DynamoDB and SQS.)"""

import uuid

import pytest

from ai_code_change_agent.orchestration.langgraph.checkpointer import postgres_checkpointer
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.ports import GateState
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from tests.integration.conftest import query
from tests.integration.test_jobs_and_changes_db import sql
from tests.unit.orchestration.test_workflow import ScriptedSteps, happy

pytestmark = pytest.mark.integration


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
