"""The single coding agent, built with the Deep Agents SDK (`create_deep_agent`).

The same agent runs in two controlled phases:
- planning: sandbox mounted read-only (the container cannot write the workspace and the backend
  refuses uploads), structured `ImplementationPlan`. The SDK's tool-level `permissions` are not
  used: deepagents 0.7 rejects them for backends with command execution, and the OS-level
  read-only mount is the actual boundary;
- implementation: writable sandbox, structured `ImplementationReport`.

The SDK's built-in tools (ls, read_file, write_file, edit_file, glob, grep, execute, write_todos,
task) all operate on the `DockerSandbox` backend. The only other tool is the injected,
harness-side `fetch_reference` (allow-listed documentation over https, executed by the worker,
ADR-011). There is no Git, GitHub or Jira access and the sandbox has no network. The agent's own
graph checkpoints under its own thread id
(it does not share the parent workflow's checkpoint).
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents._models import get_model_provider
from deepagents.backends import CompositeBackend, StateBackend
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    TodoListMiddleware,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ValidationError

from ai_code_change_agent.agents.coding.configuration import (
    AgentPhase,
    CodingAgentConfig,
    StoryContext,
)
from ai_code_change_agent.agents.coding.prompts import (
    IMPLEMENTATION_SYSTEM,
    PLANNING_SYSTEM,
    implementation_task,
    planning_task,
)
from ai_code_change_agent.agents.coding.schemas import ImplementationPlan, ImplementationReport
from ai_code_change_agent.application.errors import WorkflowError
from ai_code_change_agent.execution.docker.sandbox import DockerSandbox

# The SDK keeps oversized tool results and summarised history as files. On the sandbox backend
# those writes are refused (outside /workspace; everything in planning), so the content would stay
# in the prompt. These routes keep them in graph state instead; commands still run in the sandbox.
_SDK_STATE_ROUTES = ("/large_tool_results/", "/conversation_history/")


def _single_agent_profile(model: BaseChatModel) -> None:
    """One Deep Agent: no implicit general-purpose subagent (`task` tool) for this provider."""
    provider = get_model_provider(model) or model._llm_type
    register_harness_profile(
        provider,
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )


class AgentRunError(WorkflowError):
    code = "agent_run_failed"


_SO_ERROR = "Error: Failed to parse structured output"


class BoundStructuredOutputRetries(AgentMiddleware[Any, Any, Any]):
    """Stops a run whose final structured answer keeps failing validation. The SDK feeds each
    parse error back and retries without a limit (observed: 176 identical retries); after
    `limit` consecutive failures the run ends as agent_output_invalid instead of burning tokens."""

    def __init__(self, limit: int = 3) -> None:
        super().__init__()
        self.limit = limit

    def _check(self, state: Any) -> None:
        failures = 0
        for message in reversed(state.get("messages", []) if isinstance(state, dict) else []):
            if isinstance(message, ToolMessage):
                if str(message.content).startswith(_SO_ERROR):
                    failures += 1
                    continue
                break
            if not isinstance(message, AIMessage):
                break
        if failures >= self.limit:
            raise AgentRunError("agent_output_invalid")

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._check(state)
        return None

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._check(state)
        return None


class CodingAgent:
    def __init__(
        self,
        model: BaseChatModel,
        config: CodingAgentConfig | None = None,
        tools: Callable[[str], Sequence[BaseTool]] | None = None,
        callbacks: Callable[[str], Sequence[BaseCallbackHandler]] | None = None,
    ) -> None:
        """`tools` builds the harness-side tools afresh for every phase (per-phase budgets); the
        only one today is `fetch_reference` (allow-listed documentation, run in the worker).
        `callbacks` builds the callback handlers for one phase run from its thread id (the
        worker's LLM usage and cost capture); they see every model call of that run."""
        self._model = model
        self._config = config or CodingAgentConfig()
        self._tools = tools
        self._callbacks = callbacks

    def build(
        self,
        phase: AgentPhase,
        sandbox: DockerSandbox,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        thread_id: str = "",
    ) -> Any:
        planning = phase is AgentPhase.PLANNING
        if planning == sandbox.writable:
            raise AgentRunError("sandbox_mode_does_not_match_phase")
        _single_agent_profile(self._model)
        backend = CompositeBackend(
            default=sandbox, routes={route: StateBackend() for route in _SDK_STATE_ROUTES}
        )
        trim_old_tool_outputs = ContextEditingMiddleware(
            edits=[
                ClearToolUsesEdit(
                    trigger=self._config.clear_tool_outputs_tokens,
                    keep=5,
                    exclude_tools=("write_todos",),
                )
            ]
        )
        middleware: list[AgentMiddleware[Any, Any, Any]] = [
            trim_old_tool_outputs,
            BoundStructuredOutputRetries(),
            ModelCallLimitMiddleware(run_limit=self._config.max_model_calls, exit_behavior="error"),
        ]
        if not planning:
            middleware.append(TodoListMiddleware())
        return create_deep_agent(
            model=self._model,
            tools=list(self._tools(thread_id)) if self._tools else [],
            system_prompt=PLANNING_SYSTEM if planning else IMPLEMENTATION_SYSTEM,
            backend=backend,
            # implementation is long and multi-step: give it the built-in todo list; planning
            # already returns a structured TODO list in its plan.
            middleware=middleware,
            response_format=ImplementationPlan if planning else ImplementationReport,
            checkpointer=checkpointer,
            name=f"coding-agent-{phase.value}",
        )

    async def _run[M: BaseModel](self, agent: Any, task: str, thread_id: str, schema: type[M]) -> M:
        phase = thread_id.split(":")[2] if thread_id.count(":") >= 2 else "agent"
        config: dict[str, Any] = {
            "recursion_limit": self._config.recursion_limit,
            "configurable": {"thread_id": thread_id},
            # Tracing (LangSmith, when enabled): one thread per attempt phase; metadata is
            # inherited by every child run (model, tools, subagents).
            "run_name": f"coding-agent-{phase}",
            "tags": ["ai-code-change-agent", f"phase:{phase}"],
            "metadata": {"thread_id": thread_id, "phase": phase},
        }
        if self._callbacks is not None:
            config["callbacks"] = list(self._callbacks(thread_id))
        budget = asyncio.timeout(self._config.run_timeout_seconds)
        try:
            async with budget:
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=task)]}, config=config
                )
        except AgentRunError:
            raise  # raised by our own middleware (e.g. structured-output retries exhausted)
        except Exception as exc:  # the SDK, the model provider or a recursion limit
            guard = getattr(exc, "code", None)
            if guard in ("agent_cost_budget_exceeded",):
                raise AgentRunError(guard) from exc
            if type(exc).__name__ == "ModelCallLimitExceededError":
                raise AgentRunError("agent_model_call_limit") from exc
            if budget.expired():  # the phase's wall-clock budget, not a provider timeout
                raise AgentRunError("agent_run_timeout") from None
            raise AgentRunError(f"agent_error_{type(exc).__name__.lower()}"[:64]) from exc
        raw = result.get("structured_response") if isinstance(result, dict) else None
        try:
            return raw if isinstance(raw, schema) else schema.model_validate(raw)
        except ValidationError as exc:
            raise AgentRunError("agent_output_invalid") from exc

    async def plan(
        self,
        story: StoryContext,
        sandbox: DockerSandbox,
        *,
        thread_id: str,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> ImplementationPlan:
        agent = self.build(AgentPhase.PLANNING, sandbox, checkpointer, thread_id)
        return await self._run(agent, planning_task(story), thread_id, ImplementationPlan)

    async def implement(
        self,
        story: StoryContext,
        plan: ImplementationPlan,
        sandbox: DockerSandbox,
        *,
        thread_id: str,
        feedback: str | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> ImplementationReport:
        agent = self.build(AgentPhase.IMPLEMENTATION, sandbox, checkpointer, thread_id)
        task = implementation_task(story, plan, feedback)
        return await self._run(agent, task, thread_id, ImplementationReport)
