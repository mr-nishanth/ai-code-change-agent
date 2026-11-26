"""Run history for the studio UI (read-only).

- Studio threads: read from this Agent Server's own thread API; cost is priced from the
  provider-reported usage on each AI message (same rules as the worker's ledger).
- Jira pipeline attempts: read from the project database (attempts, usage ledger) and the
  attempt's delivery_result artifact (draft PR URL). Nothing is written."""

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_code_change_agent.application.costs.pricing import PriceTable, TokenUsage
from ai_code_change_agent.persistence.session import create_engine
from studio.workspaces import settings

router = APIRouter()
_prices = PriceTable()
_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    """The project database (read-only use)."""
    global _engine
    url = settings().database_url
    if url is None:
        raise RuntimeError("DATABASE_URL not configured")
    if _engine is None:
        _engine = create_engine(url.get_secret_value())
    return _engine


def usage_of_message(msg: dict[str, Any]) -> tuple[str, TokenUsage] | None:
    """Provider-reported usage of one serialized AI message (LangChain input_tokens includes
    cache reads and writes; the uncached remainder is the input)."""
    if msg.get("type") != "ai":
        return None
    u = msg.get("usage_metadata")
    if not isinstance(u, dict) or "input_tokens" not in u:
        return None
    d = u.get("input_token_details") or {}
    read = int(d.get("cache_read") or 0)
    w5, w1 = d.get("ephemeral_5m_input_tokens"), d.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = d.get("cache_creation") or 0, 0
    w5, w1 = int(w5 or 0), int(w1 or 0)
    model = str((msg.get("response_metadata") or {}).get("model") or settings().llm_model or "")
    usage = TokenUsage(
        max(int(u["input_tokens"]) - read - w5 - w1, 0),
        int(u.get("output_tokens") or 0),
        w5,
        w1,
        read,
    )
    return model, usage


def _text(msg: dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def summarize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    values = thread.get("values") or {}
    messages = values.get("messages") or [] if isinstance(values, dict) else []
    first = next((m for m in messages if m.get("type") == "human"), None)
    total, calls, unpriced, tokens_in, tokens_out = Decimal(0), 0, 0, 0, 0
    for m in messages:
        found = usage_of_message(m)
        if found is None:
            continue
        model, usage = found
        calls += 1
        tokens_in += usage.prompt_tokens
        tokens_out += usage.output_tokens
        priced = _prices.price(model, usage)
        if priced is None:
            unpriced += 1
        else:
            total += priced.total
    result = values.get("structured_response") if isinstance(values, dict) else None
    todos = values.get("todos") or [] if isinstance(values, dict) else []
    return {
        "thread_id": thread["thread_id"],
        "assistant": (thread.get("metadata") or {}).get("graph_id"),
        "status": thread.get("status"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "title": (_text(first) if first else "")[:140],
        "outcome": (result.get("outcome") or "plan") if isinstance(result, dict) else None,
        "todos_done": sum(1 for t in todos if t.get("status") == "completed"),
        "todos_total": len(todos),
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": f"{total:.6f}",
        "unpriced_calls": unpriced,
    }


@router.get("/studio/threads")
async def threads(request: Request, limit: int = 50) -> dict[str, Any]:
    body = {
        "limit": max(1, min(limit, 200)),
        "sort_by": "updated_at",
        "sort_order": "desc",
        "select": ["thread_id", "created_at", "updated_at", "metadata", "status", "values"],
    }
    async with httpx.AsyncClient(base_url=str(request.base_url), timeout=20) as client:
        res = await client.post("/threads/search", json=body)
        res.raise_for_status()
    return {
        "threads": [summarize_thread(t) for t in res.json()],
        "pricing_version": _prices.version,
    }


_ATTEMPTS = text(
    """
SELECT a.id, a.number, a.status, a.failure_code, a.repair_count, a.created_at, a.updated_at,
       r.jira_issue_key, repo.repository_key
FROM code_change_attempts a
JOIN code_change_requests r ON r.id = a.request_id
JOIN repositories repo ON repo.id = a.repository_id
ORDER BY a.created_at DESC
LIMIT :limit
"""
)
_USAGE = text(
    """
SELECT attempt_id, stage, model, input_tokens, output_tokens, cache_write_5m_tokens,
       cache_write_1h_tokens, cache_read_tokens, cost_usd
FROM llm_usage_records WHERE attempt_id = ANY(:ids)
"""
)


def _pr_url(request_id: str | None, attempt_id: str) -> str | None:
    root = settings().artifact_root
    if not root or not request_id:
        return None
    folder = Path(root) / request_id / attempt_id
    if not folder.is_dir() or folder.is_symlink():
        return None
    for meta_path in folder.glob("*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("kind") != "delivery_result":
                continue
            blob = json.loads((folder / f"{meta['id']}.blob").read_text(encoding="utf-8"))
            url = blob.get("pr_url")
            if isinstance(url, str) and url.startswith("https://github.com/"):
                return url
        except (OSError, ValueError, KeyError):
            continue
    return None


@router.get("/studio/attempts")
async def attempts(limit: int = 50) -> dict[str, Any]:
    if settings().database_url is None:
        return {"attempts": [], "error": "DATABASE_URL not configured"}
    try:
        async with engine().connect() as conn:
            rows = (
                (await conn.execute(_ATTEMPTS, {"limit": max(1, min(limit, 200))})).mappings().all()
            )
            ids = [r["id"] for r in rows]
            usage = (await conn.execute(_USAGE, {"ids": ids})).mappings().all() if ids else []
            req = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, request_id FROM code_change_attempts WHERE id = ANY(:ids)"
                        ),
                        {"ids": ids},
                    )
                )
                .mappings()
                .all()
                if ids
                else []
            )
    except Exception as exc:  # the studio must work without the pipeline database
        return {"attempts": [], "error": f"database unavailable: {type(exc).__name__}"}
    request_of = {str(r["id"]): str(r["request_id"]) for r in req}
    per: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "calls": 0,
            "unknown": 0,
            "cost": Decimal(0),
            "in": 0,
            "out": 0,
            "stages": defaultdict(Decimal),
            "models": set(),
        }
    )
    for u in usage:
        p = per[str(u["attempt_id"])]
        p["calls"] += 1
        p["models"].add(u["model"])
        if u["input_tokens"] is None or u["cost_usd"] is None:
            p["unknown"] += 1
            continue
        p["cost"] += u["cost_usd"]
        p["stages"][u["stage"]] += u["cost_usd"]
        p["in"] += (
            u["input_tokens"]
            + (u["cache_read_tokens"] or 0)
            + (u["cache_write_5m_tokens"] or 0)
            + (u["cache_write_1h_tokens"] or 0)
        )
        p["out"] += u["output_tokens"]
    out = []
    for r in rows:
        aid = str(r["id"])
        stats = per.get(aid)
        out.append(
            {
                "attempt_id": aid,
                "issue": r["jira_issue_key"],
                "number": r["number"],
                "repository": r["repository_key"],
                "status": r["status"],
                "failure_code": r["failure_code"],
                "repairs": r["repair_count"],
                "created_at": r["created_at"].isoformat(),
                "updated_at": r["updated_at"].isoformat(),
                "calls": stats["calls"] if stats else 0,
                "unknown_usage_calls": stats["unknown"] if stats else 0,
                "tokens_in": stats["in"] if stats else 0,
                "tokens_out": stats["out"] if stats else 0,
                # None: no ledger yet (attempts before cost tracking)
                "cost_usd": f"{stats['cost']:.6f}" if stats else None,
                "stage_costs": {k: f"{v:.6f}" for k, v in stats["stages"].items()} if stats else {},
                "models": sorted(stats["models"]) if stats else [],
                "pr_url": _pr_url(request_of.get(aid), aid),
            }
        )
    return {"attempts": out, "pricing_version": _prices.version}
