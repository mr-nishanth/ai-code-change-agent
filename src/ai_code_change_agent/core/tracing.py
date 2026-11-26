"""LangSmith tracing for the worker (the API never traces).

LangChain, LangGraph and Deep Agents trace automatically through the LangSmith SDK, which reads the
process environment. Settings load `.env` without exporting it, so the worker exports exactly the
tracing variables here, and removes legacy destinations that could send traces elsewhere."""

import os
from collections.abc import MutableMapping

from ai_code_change_agent.core.config import Settings

_LEGACY = ("LANGCHAIN_PROJECT", "LANGCHAIN_SESSION", "LANGCHAIN_TRACING_V2", "LANGCHAIN_ENDPOINT")
_MANAGED = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_WORKSPACE_ID",
    "LANGSMITH_AGENT_ID",
    "LANGSMITH_AGENT_ENVIRONMENT",
    "LANGSMITH_PROJECT",
    "LANGSMITH_HIDE_INPUTS",
    "LANGSMITH_HIDE_OUTPUTS",
)


def configure_tracing(settings: Settings, env: MutableMapping[str, str] | None = None) -> bool:
    """Export the tracing configuration for this process; returns whether tracing is on."""
    target = os.environ if env is None else env
    for key in (*_LEGACY, *_MANAGED):
        target.pop(key, None)
    if not settings.langsmith_tracing or settings.langsmith_api_key is None:
        target["LANGSMITH_TRACING"] = "false"
        return False
    values = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": settings.langsmith_api_key.get_secret_value(),
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
        "LANGSMITH_WORKSPACE_ID": settings.langsmith_workspace_id,
        "LANGSMITH_HIDE_INPUTS": "true" if settings.langsmith_hide_inputs else None,
        "LANGSMITH_HIDE_OUTPUTS": "true" if settings.langsmith_hide_outputs else None,
    }
    if settings.langsmith_agent_id:
        values["LANGSMITH_AGENT_ID"] = settings.langsmith_agent_id
        values["LANGSMITH_AGENT_ENVIRONMENT"] = settings.langsmith_agent_environment
    else:
        values["LANGSMITH_PROJECT"] = settings.langsmith_project or settings.app_name
    target.update({k: v for k, v in values.items() if v})
    return True


def flush_traces() -> None:
    """Send queued traces before the process exits (the exporter works in the background)."""
    from langchain_core.tracers.langchain import wait_for_all_tracers

    wait_for_all_tracers()
