"""Graph factories for `langgraph dev`: the project's own CodingAgent, one per phase.

- `coding_planner`: planning phase, read-only sandbox, structured ImplementationPlan.
- `coding_agent`: implementation phase, writable sandbox, write_todos, ImplementationReport.

Each run starts a hardened sandbox container on the thread's workspace and removes it when the
run ends; the workspace (files) persists for the thread. The single-agent design is unchanged:
the `task` subagent tool is disabled, so the UI's subagent panel stays empty for these graphs."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph_sdk.runtime import ServerRuntime

from ai_code_change_agent.agents.coding.agent import CodingAgent
from ai_code_change_agent.agents.coding.configuration import AgentPhase, CodingAgentConfig
from ai_code_change_agent.composition import build_reference_tools
from ai_code_change_agent.providers.llm.factory import build_chat_model
from studio.workspaces import placeholder_sandbox, sandboxes, settings, workspace_for

_agent: CodingAgent | None = None


def _coding_agent() -> CodingAgent:
    global _agent
    if _agent is None:
        s = settings()
        _agent = CodingAgent(
            build_chat_model(s),
            CodingAgentConfig(
                recursion_limit=s.deep_agent_recursion_limit,
                run_timeout_seconds=s.deep_agent_run_timeout_seconds,
                clear_tool_outputs_tokens=s.llm_context_tokens // 2,
            ),
            build_reference_tools(s),
        )
    return _agent


@contextlib.asynccontextmanager
async def _graph(
    phase: AgentPhase,
    config: RunnableConfig,
    runtime: ServerRuntime,  # type: ignore[type-arg]
) -> AsyncIterator[Any]:
    writable = phase is AgentPhase.IMPLEMENTATION
    agent = _coding_agent()
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    if runtime.execution_runtime is None or not thread_id:
        yield agent.build(phase, placeholder_sandbox(writable))  # same topology, no container
        return
    ws = await workspace_for(thread_id)
    assert ws is not None  # noqa: S101
    sandbox = await sandboxes().start(ws.handle, ws.work, writable=writable)
    try:
        yield agent.build(phase, sandbox)
    finally:
        await sandboxes().stop(sandbox)


@contextlib.asynccontextmanager
async def make_planner(
    config: RunnableConfig,
    runtime: ServerRuntime,  # type: ignore[type-arg]
) -> AsyncIterator[Any]:
    async with _graph(AgentPhase.PLANNING, config, runtime) as graph:
        yield graph


@contextlib.asynccontextmanager
async def make_coder(
    config: RunnableConfig,
    runtime: ServerRuntime,  # type: ignore[type-arg]
) -> AsyncIterator[Any]:
    async with _graph(AgentPhase.IMPLEMENTATION, config, runtime) as graph:
        yield graph
