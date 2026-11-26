"""Starting and resuming the workflow for one attempt (thread id = attempt id)."""

import uuid
from typing import Any

from langgraph.types import Command


def _config(attempt_id: uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"attempt:{attempt_id}"}}


class WorkflowRunner:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def start(self, attempt_id: uuid.UUID) -> dict[str, Any]:
        state = await self._graph.aget_state(_config(attempt_id))
        if state.values:  # already started: never run twice; resume instead
            return await self.resume(attempt_id)
        result: dict[str, Any] = await self._graph.ainvoke(
            {"attempt_id": str(attempt_id)}, config=_config(attempt_id)
        )
        return result

    async def resume(self, attempt_id: uuid.UUID) -> dict[str, Any]:
        result: dict[str, Any] = await self._graph.ainvoke(
            Command(resume=True), config=_config(attempt_id)
        )
        return result

    async def waiting_for(self, attempt_id: uuid.UUID) -> str | None:
        state = await self._graph.aget_state(_config(attempt_id))
        for task in state.tasks:
            for it in task.interrupts:
                value = it.value
                if isinstance(value, dict) and "awaiting" in value:
                    return str(value["awaiting"])
        return None
