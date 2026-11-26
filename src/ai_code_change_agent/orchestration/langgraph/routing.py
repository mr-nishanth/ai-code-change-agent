"""Edges: decided only by the persisted attempt status a step returned."""

from collections.abc import Callable

from ai_code_change_agent.orchestration.langgraph.state import WorkflowState

STOP = "cleanup"


def after(expected: str, nxt: str) -> Callable[[WorkflowState], str]:
    def route(state: WorkflowState) -> str:
        return nxt if state.get("status") == expected else STOP

    return route


def after_validate(state: WorkflowState) -> str:
    status = state.get("status")
    if status == "AWAITING_FINAL_APPROVAL":
        return "final_gate"
    if status == "IMPLEMENTING":  # a bounded repair: the domain already counted it
        return "implement"
    return STOP
