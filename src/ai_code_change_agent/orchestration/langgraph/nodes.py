"""Graph nodes: thin adapters from WorkflowState to WorkflowSteps calls."""

import uuid
from typing import Any

from langgraph.types import interrupt

from ai_code_change_agent.orchestration.langgraph.ports import GateState, StepResult, WorkflowSteps
from ai_code_change_agent.orchestration.langgraph.state import WorkflowState


def _out(result: StepResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "feedback": result.feedback,
        "failure_code": result.failure_code,
    }


class Nodes:
    def __init__(self, steps: WorkflowSteps) -> None:
        self._steps = steps

    @staticmethod
    def _id(state: WorkflowState) -> uuid.UUID:
        return uuid.UUID(state["attempt_id"])

    async def prepare(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.prepare(self._id(state)))

    async def plan(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.plan(self._id(state)))

    async def _gate(self, state: WorkflowState, gate: str) -> dict[str, Any]:
        """Pause until an operator decided. The resume value is only a wake-up signal: the
        decision is read again from the domain (hash-bound approval records)."""
        while True:
            decision = await self._steps.gate(self._id(state), gate)
            if decision is GateState.APPROVED:
                return {"status": f"{gate}_APPROVED"}
            if decision is GateState.REJECTED:
                return {"status": "REJECTED"}
            interrupt({"attempt_id": state["attempt_id"], "awaiting": gate})

    async def plan_gate(self, state: WorkflowState) -> dict[str, Any]:
        return await self._gate(state, "PLAN")

    async def implement(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.implement(self._id(state), state.get("feedback")))

    async def validate(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.validate(self._id(state)))

    async def final_gate(self, state: WorkflowState) -> dict[str, Any]:
        return await self._gate(state, "FINAL")

    async def deliver(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.deliver(self._id(state)))

    async def report(self, state: WorkflowState) -> dict[str, Any]:
        return _out(await self._steps.report(self._id(state)))

    async def cleanup(self, state: WorkflowState) -> dict[str, Any]:
        await self._steps.cleanup(self._id(state))
        return {}
