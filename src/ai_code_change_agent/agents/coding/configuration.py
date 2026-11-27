"""Configuration and inputs of the coding agent."""

from dataclasses import dataclass
from enum import StrEnum


class AgentPhase(StrEnum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"


@dataclass(frozen=True)
class CodingAgentConfig:
    recursion_limit: int = 250
    # wall-clock budget for one phase (all model and tool calls together)
    run_timeout_seconds: float = 5400.0
    # Clear old tool outputs (no model call) once the prompt exceeds this many tokens; keeps the
    # last five results and the todo list. Summarisation (built in) handles the rest.
    clear_tool_outputs_tokens: int = 60_000
    # Hard guards against runaway loops, for any model: model calls per phase run (the SDK's
    # recursion limit counts graph steps, not paid calls) and a USD budget per phase run.
    max_model_calls: int = 80
    max_phase_cost_usd: float | None = 2.0


@dataclass(frozen=True)
class MultiRepoBrief:
    """This repository's part in a multi-repository Story (read-only context)."""

    role: str | None
    responsibility: str | None
    siblings: tuple[str, ...]  # other repositories, changed by separate isolated runs
    downstream: tuple[str, ...]  # repositories that consume this one's published contract
    upstream_contracts: tuple[tuple[str, str, str], ...] = ()  # (repository key, sha256, text)


@dataclass(frozen=True)
class StoryContext:
    """Verified inputs for one run: Story text (untrusted) and platform-resolved identifiers."""

    issue_key: str
    summary: str
    description: str | None
    acceptance_criteria: tuple[str, ...]
    repository_key: str
    base_commit_sha: str
    validation_commands: tuple[str, ...] = ()
    multi_repo: MultiRepoBrief | None = None
    repository_dependencies: tuple[str, ...] = ()
