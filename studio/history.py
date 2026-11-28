"""Run history for the studio UI (read-only).

- Studio threads: read from this Agent Server's own thread API; cost is priced from the
  provider-reported usage on each AI message (same rules as the worker's ledger).
- Jira pipeline attempts: read through the application's ports (`studio.stores`): the attempt
  listing (S1), request and repository (C1, R4), the usage ledger (`CostQuery`, U2) and the
  attempt's delivery_result artifact (`ArtifactStore`, S4) for the draft PR URL. Nothing is
  written."""

import asyncio
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, Request

from ai_code_change_agent.application.artifacts.errors import ArtifactError
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.artifacts.ports import ArtifactStore
from ai_code_change_agent.application.changes.ports import AttemptListing
from ai_code_change_agent.application.costs.pricing import PriceTable, TokenUsage
from ai_code_change_agent.application.costs.report import CostRow
from ai_code_change_agent.domain.changes.models import CodeChangeAttempt
from studio.stores import StudioStores, stores
from studio.workspaces import settings

router = APIRouter()
_prices = PriceTable()
MAX_ATTEMPTS = 200


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


async def attempt_names(
    st: StudioStores, attempts: Iterable[CodeChangeAttempt]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """Jira issue key per request (C1) and repository key per repository (R4)."""
    attempts = list(attempts)
    request_ids = list(dict.fromkeys(a.request_id for a in attempts))
    requests = await asyncio.gather(*(st.changes.get_request(r) for r in request_ids))
    issues = {r.id: r.jira_issue_key for r in requests}
    repositories: dict[uuid.UUID, str] = {}
    async with st.registry() as uow:
        for repository_id in dict.fromkeys(a.repository_id for a in attempts):
            repo = await uow.store.get_repository_by_id(repository_id)
            if repo is not None:
                repositories[repository_id] = repo.repository_key
    return issues, repositories


def attempt_header(
    listing: AttemptListing, issues: dict[uuid.UUID, str], repositories: dict[uuid.UUID, str]
) -> dict[str, Any]:
    a = listing.attempt
    return {
        "attempt_id": str(a.id),
        "issue": issues.get(a.request_id),
        "number": a.number,
        "repository": repositories.get(a.repository_id),
        "status": a.status.value,
        "failure_code": a.failure_code,
        "repairs": a.repair_count,
        "created_at": listing.created_at.isoformat(),
        "updated_at": listing.updated_at.isoformat(),
    }


async def pr_url(artifacts: ArtifactStore, attempt: CodeChangeAttempt) -> str | None:
    """The draft PR link from the attempt's newest delivery_result artifact (re-hashed on read)."""
    scope = ArtifactScope(attempt.request_id, attempt.id)
    try:
        metas = await artifacts.list(scope)
        for meta in sorted(metas, key=lambda m: m.created_at, reverse=True):
            if meta.kind is not ArtifactKind.DELIVERY_RESULT:
                continue
            blob = json.loads(await artifacts.get(scope, meta.id))
            url = blob.get("pr_url") if isinstance(blob, dict) else None
            if isinstance(url, str) and url.startswith("https://github.com/"):
                return url
    except (ArtifactError, ValueError):
        return None
    return None


def _usage_stats(rows: Sequence[CostRow]) -> dict[uuid.UUID, dict[str, Any]]:
    per: dict[uuid.UUID, dict[str, Any]] = defaultdict(
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
    for row in rows:
        record = row.record
        if record.attempt_id is None:
            continue
        p = per[record.attempt_id]
        p["calls"] += 1
        p["models"].add(record.model)
        if record.usage is None or record.cost_usd is None:
            p["unknown"] += 1
            continue
        p["cost"] += record.cost_usd
        p["stages"][record.stage] += record.cost_usd
        p["in"] += record.usage.prompt_tokens
        p["out"] += record.usage.output_tokens
    return per


@router.get("/studio/attempts")
async def attempts(limit: int = 50) -> dict[str, Any]:
    try:
        st = stores()
        listings = await st.changes.recent_attempts(max(1, min(limit, MAX_ATTEMPTS)))
        found = [x.attempt for x in listings]
        issues, repositories = await attempt_names(st, found)
        usage = await st.costs.rows(attempt_ids=[a.id for a in found]) if found else []
        links = await asyncio.gather(*(pr_url(st.artifacts, a) for a in found))
    except Exception as exc:  # the studio must work without the pipeline stores
        return {"attempts": [], "error": f"store unavailable: {type(exc).__name__}"}
    per = _usage_stats(usage)
    out = []
    for listing, link in zip(listings, links, strict=True):
        stats = per.get(listing.attempt.id)
        out.append(
            {
                **attempt_header(listing, issues, repositories),
                "calls": stats["calls"] if stats else 0,
                "unknown_usage_calls": stats["unknown"] if stats else 0,
                "tokens_in": stats["in"] if stats else 0,
                "tokens_out": stats["out"] if stats else 0,
                # None: no ledger yet (attempts before cost tracking)
                "cost_usd": f"{stats['cost']:.6f}" if stats else None,
                "stage_costs": {k: f"{v:.6f}" for k, v in stats["stages"].items()} if stats else {},
                "models": sorted(stats["models"]) if stats else [],
                "pr_url": link,
            }
        )
    return {"attempts": out, "pricing_version": _prices.version}
