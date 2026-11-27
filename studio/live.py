"""Live, read-only view of a Jira pipeline attempt for the studio UI.

The worker runs the Deep Agent in its own process; every agent step is checkpointed to DynamoDB
(`DynamoDBSaver`) under thread ids `attempt:<id>:planning:<run>` and
`attempt:<id>:implementation:<repairs>:<run>`, and each run's thread id is indexed under its attempt
when the run starts (L3). These routes read that index, the checkpoints (L4, read-only), the
attempt header (S3) and the worker's live workspace, so the UI can poll them while the attempt runs.
Nothing is written; the worker is not affected."""

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from langchain_core.messages import BaseMessage, messages_to_dict
from pydantic import BaseModel

from ai_code_change_agent.agents.coding.configuration import AgentPhase
from ai_code_change_agent.domain.changes.errors import ChangeNotFoundError
from studio.agent_graph import _coding_agent
from studio.api_files import file_view, tree_view
from studio.history import attempt_header, attempt_names
from studio.stores import StudioStores, stores
from studio.workspaces import placeholder_sandbox, settings

router = APIRouter()
_THREAD = re.compile(r"attempt:[0-9a-f-]{36}:(planning|implementation)(:\d+)?:[0-9a-f]{8}")
_TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "BLOCKED", "CANCELLED"}


def _phase(thread_id: str) -> dict[str, Any]:
    parts = thread_id.split(":")
    phase = parts[2]
    repairs = int(parts[3]) if phase == "implementation" else 0
    stage = "correction" if repairs else phase
    return {"thread_id": thread_id, "phase": phase, "stage": stage, "repairs": repairs}


async def _attempt_row(st: StudioStores, attempt_id: uuid.UUID) -> dict[str, Any]:
    try:
        listing = await st.changes.attempt_listing(attempt_id)
    except ChangeNotFoundError:
        raise HTTPException(404, "attempt_not_found") from None
    issues, repositories = await attempt_names(st, [listing.attempt])
    return attempt_header(listing, issues, repositories)


@router.get("/studio/attempts/{attempt_id}/live")
async def live(attempt_id: uuid.UUID) -> dict[str, Any]:
    st = stores()
    attempt = await _attempt_row(st, attempt_id)
    prefix = f"attempt:{attempt_id}:"
    threads = [
        t
        for t in await st.changes.agent_threads(attempt_id)
        if t.startswith(prefix) and _THREAD.fullmatch(t)
    ]
    phases = []
    graphs: dict[str, Any] = {}
    for tid in threads:
        info = _phase(tid)
        phase = AgentPhase(info["phase"])
        if phase.value not in graphs:  # the same agent graph rebuilds state from the checkpoints
            graphs[phase.value] = _coding_agent().build(
                phase,
                placeholder_sandbox(phase is AgentPhase.IMPLEMENTATION),
                checkpointer=st.checkpointer,
            )
        state = await graphs[phase.value].aget_state({"configurable": {"thread_id": tid}})
        values = state.values or {}
        messages = [m for m in values.get("messages") or [] if isinstance(m, BaseMessage)]
        raw = values.get("structured_response")
        phases.append(
            {
                **info,
                "messages": messages_to_dict(messages),
                "todos": values.get("todos") or [],
                "result": raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw,
                "running": bool(state.next),
                "updated_at": state.created_at,
            }
        )
    return {"attempt": attempt, "phases": phases, "terminal": attempt["status"] in _TERMINAL}


def _worker_workspace(attempt_id: uuid.UUID) -> Path | None:
    root = settings().workspace_root
    if not root:
        return None
    attempt_dir = Path(root) / str(attempt_id)
    if not attempt_dir.is_dir() or attempt_dir.is_symlink():
        return None
    candidates = sorted(
        (p for p in attempt_dir.glob("ws_*") if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


@router.get("/studio/attempts/{attempt_id}/tree")
async def attempt_tree(attempt_id: uuid.UUID) -> dict[str, Any]:
    ws = _worker_workspace(attempt_id)
    if ws is None:
        raise HTTPException(404, "no_live_workspace")  # exists only while a phase runs
    return tree_view(ws / "work", ws / "baseline", ws.name)


@router.get("/studio/attempts/{attempt_id}/file")
async def attempt_file(attempt_id: uuid.UUID, path: str) -> dict[str, Any]:
    ws = _worker_workspace(attempt_id)
    if ws is None:
        raise HTTPException(404, "no_live_workspace")
    return file_view(ws / "work", ws / "baseline", path)
