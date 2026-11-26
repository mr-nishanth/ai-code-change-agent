"""The one parent LangGraph workflow.

prepare -> plan -> plan_gate (interrupt) -> implement -> validate -> [implement (repair) |
final_gate (interrupt)] -> deliver -> report -> cleanup. Any other outcome goes to cleanup.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ai_code_change_agent.orchestration.langgraph.nodes import Nodes
from ai_code_change_agent.orchestration.langgraph.ports import WorkflowSteps
from ai_code_change_agent.orchestration.langgraph.routing import STOP, after, after_validate
from ai_code_change_agent.orchestration.langgraph.state import WorkflowState


def build_graph(steps: WorkflowSteps, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    n = Nodes(steps)
    g = StateGraph(WorkflowState)
    for name in (
        "prepare",
        "plan",
        "plan_gate",
        "implement",
        "validate",
        "final_gate",
        "deliver",
        "report",
        "cleanup",
    ):
        g.add_node(name, getattr(n, name))
    g.add_edge(START, "prepare")
    g.add_conditional_edges("prepare", after("PLANNING", "plan"), ["plan", STOP])
    g.add_conditional_edges(
        "plan", after("AWAITING_PLAN_APPROVAL", "plan_gate"), ["plan_gate", STOP]
    )
    g.add_conditional_edges("plan_gate", after("PLAN_APPROVED", "implement"), ["implement", STOP])
    g.add_conditional_edges("implement", after("VALIDATING", "validate"), ["validate", STOP])
    g.add_conditional_edges("validate", after_validate, ["implement", "final_gate", STOP])
    g.add_conditional_edges("final_gate", after("FINAL_APPROVED", "deliver"), ["deliver", STOP])
    g.add_conditional_edges("deliver", after("REPORTING", "report"), ["report", STOP])
    g.add_edge("report", STOP)
    g.add_edge(STOP, END)
    return g.compile(checkpointer=checkpointer, name="code-change-workflow")
