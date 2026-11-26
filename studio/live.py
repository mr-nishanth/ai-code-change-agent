"""Live, read-only view of a Jira pipeline attempt for the studio UI.

The worker runs the Deep Agent in its own process; every agent step is checkpointed to PostgreSQL
under thread ids `attempt:<id>:planning:<run>` and `attempt:<id>:implementation:<repairs>:<run>`.
These routes read those checkpoints, the attempt row and the worker's live workspace, so the UI
can poll them while the attempt runs. Nothing is written; the worker is not affected."""

import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException
from langchain_core.messages import BaseMessage, messages_to_dict
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from pydantic import BaseModel
from sqlalchemy import text

from ai_code_change_agent.agents.coding.configuration import AgentPhase
from ai_code_change_agent.orchestration.langgraph.checkpointer import libpq_url
from studio.agent_graph import _coding_agent
from studio.api_files import file_view, tree_view
from studio.history import engine
from studio.workspaces import placeholder_sandbox, settings

router = APIRouter()
_THREAD = re.compile(r"attempt:[0-9a-f-]{36}:(planning|implementation)(:\d+)?:[0-9a-f]{8}")
_conn: psycopg.AsyncConnection[Any] | None = None


async def _saver() -> AsyncPostgresSaver:
    global _conn
    url = settings().database_url
    if url is None:
        raise HTTPException(503, "DATABASE_URL not configured")
    if _conn is None or _conn.closed:
        _conn = await psycopg.AsyncConnection.connect(
            libpq_url(url.get_secret_value()),
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
            options=f"-c search_path={settings().langgraph_checkpoint_schema}",
        )
    return AsyncPostgresSaver(_conn)  # read-only use: no setup(), no writes


def _phase(thread_id: str) -> dict[str, Any]:
    parts = thread_id.split(":")
    phase = parts[2]
    repairs = int(parts[3]) if phase == "implementation" else 0
    stage = "correction" if repairs else phase
    return {"thread_id": thread_id, "phase": phase, "stage": stage, "repairs": repairs}


async def _attempt_row(attempt_id: uuid.UUID) -> dict[str, Any]:
    q = text(
        "SELECT a.id, a.number, a.status, a.failure_code, a.repair_count, a.created_at, "
        "a.updated_at, r.jira_issue_key, repo.repository_key FROM code_change_attempts a "
        "JOIN code_change_requests r ON r.id = a.request_id "
        "JOIN repositories repo ON repo.id = a.repository_id WHERE a.id = :id"
    )
    async with engine().connect() as conn:
        row = (await conn.execute(q, {"id": attempt_id})).mappings().first()
    if row is None:
        raise HTTPException(404, "attempt_not_found")
    return {
        "attempt_id": str(row["id"]),
        "issue": row["jira_issue_key"],
        "number": row["number"],
        "status": row["status"],
        "failure_code": row["failure_code"],
        "repairs": row["repair_count"],
        "repository": row["repository_key"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/studio/attempts/{attempt_id}/live")
async def live(attempt_id: uuid.UUID) -> dict[str, Any]:
    attempt = await _attempt_row(attempt_id)
    saver = await _saver()
    assert saver.conn is not None  # noqa: S101
    async with saver.conn.cursor() as cur:  # type: ignore[union-attr]
        await cur.execute(
            "SELECT thread_id, max(checkpoint_id) AS last FROM checkpoints "
            "WHERE thread_id LIKE %s AND checkpoint_ns = '' GROUP BY thread_id ORDER BY last",
            (f"attempt:{attempt_id}:%",),
        )
        threads = [
            r["thread_id"] for r in await cur.fetchall() if _THREAD.fullmatch(r["thread_id"])
        ]
    phases = []
    graphs: dict[str, Any] = {}
    for tid in threads:
        info = _phase(tid)
        phase = AgentPhase(info["phase"])
        if phase.value not in graphs:  # the same agent graph rebuilds state from the checkpoints
            graphs[phase.value] = _coding_agent().build(
                phase, placeholder_sandbox(phase is AgentPhase.IMPLEMENTATION), checkpointer=saver
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
    terminal = attempt["status"] in {"COMPLETED", "FAILED", "REJECTED", "BLOCKED", "CANCELLED"}
    return {"attempt": attempt, "phases": phases, "terminal": terminal}


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
