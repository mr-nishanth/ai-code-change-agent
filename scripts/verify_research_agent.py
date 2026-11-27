"""Live check: does the real planning agent use search_docs and fetch_reference by itself?

Makes REAL model calls (cost: a few cents with claude-haiku-5-5) and needs Docker, PostgreSQL and
the execution image. It never writes to Jira or GitHub. Usage:

    uv run python scripts/verify_research_agent.py <path-to-a-python-repo-checkout>

(trusted hosts come from config/reference_sources.yaml)

Prints the registered tools, the plan, and the session's cost-ledger rows; the worker log events
`agent_tool_call` show each tool invocation with its outcome, URLs and retrieval time.
"""

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from ai_code_change_agent.agents.coding.agent import CodingAgent
from ai_code_change_agent.agents.coding.configuration import CodingAgentConfig, StoryContext
from ai_code_change_agent.application.costs.usage import UsageTracker
from ai_code_change_agent.application.workspaces.models import WorkspaceHandle
from ai_code_change_agent.composition import (
    build_chat_model,
    build_price_table,
    build_reference_tools,
    build_sandbox_manager,
)
from ai_code_change_agent.core.config import get_settings
from ai_code_change_agent.persistence.repositories.usage_store import PostgresUsageStore
from ai_code_change_agent.persistence.session import create_engine, create_session_factory


async def main(work: str) -> None:
    s = get_settings()
    engine = create_engine(s.database_url.get_secret_value())
    sessions = create_session_factory(engine)
    attempt = uuid.uuid4()
    usage = UsageTracker(
        PostgresUsageStore(sessions),
        build_price_table(s),
        provider=s.llm_provider,
        model=s.llm_model or "-",
        phase_budget_usd=Decimal("1.00"),
    )
    tools = build_reference_tools(s, usage)
    probe = [t.name for t in tools("x")] if tools else []
    print(
        json.dumps(
            {
                "event": "registered_tools",
                "tools": probe,
                "domains": sorted(s.reference_allowed_hosts),
            }
        ),
        flush=True,
    )
    agent = CodingAgent(
        build_chat_model(s),
        CodingAgentConfig(max_model_calls=30, max_phase_cost_usd=1.0, run_timeout_seconds=900),
        tools,
        usage.handlers,
    )
    handle = WorkspaceHandle(
        id="research-e2e",
        request_id=uuid.uuid4(),
        attempt_id=attempt,
        repository_id=uuid.uuid4(),
        profile_version_id=uuid.uuid4(),
        base_commit_sha="0" * 40,
        baseline_tree_hash="0" * 40,
        created_at=datetime.now(UTC),
    )
    mgr = build_sandbox_manager(s)
    sandbox = await mgr.start(handle, Path(work), writable=False)
    story = StoryContext(
        issue_key="RESEARCH-1",
        summary="Add an application startup/shutdown hook to the FastAPI app",
        description=(
            "The service must open and close a shared httpx.AsyncClient when the app starts "
            "and stops. Use the mechanism the CURRENT official FastAPI documentation recommends "
            "for startup and shutdown (check whether @app.on_event is deprecated). Cite the "
            "official documentation page you relied on."
        ),
        acceptance_criteria=(
            "Uses the currently recommended FastAPI startup/shutdown API",
            "The plan cites the official FastAPI documentation URL and its retrieval date",
        ),
        repository_key="automate_connector",
        base_commit_sha="0" * 40,
    )
    thread = f"research-e2e:{uuid.uuid4().hex[:8]}"
    try:
        plan = await agent.plan(story, sandbox, thread_id=thread)
        print(
            json.dumps({"event": "plan", "plan": plan.model_dump(mode="json")})[:6000], flush=True
        )
    finally:
        await mgr.stop(sandbox)
    async with engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "select stage, model, succeeded, cost_usd, input_tokens, output_tokens "
                    "from llm_usage_records where session_id = :a order by created_at"
                ),
                {"a": usage.session_id},
            )
        ).all()
        print(
            json.dumps(
                {
                    "event": "ledger",
                    "session": str(usage.session_id),
                    "thread": thread,
                    "rows": [[str(x) for x in r] for r in rows],
                }
            ),
            flush=True,
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
