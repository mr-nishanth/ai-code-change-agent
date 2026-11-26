"""Custom routes served by the Agent Server (`http.app`): browse a thread's sandbox workspace
and price token usage with the project's single price table. Read-only; paths are confined to the
thread's workspace and symlinks are never followed."""

from decimal import Decimal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ai_code_change_agent.application.costs.pricing import PriceTable, TokenUsage
from studio.api_files import file_view, tree_view
from studio.history import router as history_router
from studio.live import router as live_router
from studio.workspaces import SEED_DIR, StudioWorkspace, settings, workspace_for

app = FastAPI(title="deep-agent-studio")
app.include_router(history_router)
app.include_router(live_router)
_prices = PriceTable()


async def _ws(thread_id: str) -> StudioWorkspace:
    ws = await workspace_for(thread_id, create=False)  # created by the first run
    if ws is None:
        raise HTTPException(404, "workspace_not_started")
    return ws


@app.get("/studio/info")
async def info() -> dict[str, object]:
    s = settings()
    return {
        "provider": s.llm_provider,
        "model": s.llm_model,
        "seed": SEED_DIR.name,
        "image": s.workspace_image,
        "pricing_version": _prices.version,
        "assistants": {
            "coding_planner": "planning phase: read-only sandbox, ImplementationPlan",
            "coding_agent": "implementation phase: writable sandbox, todos, ImplementationReport",
        },
    }


@app.get("/sandbox/{thread_id}/tree")
async def tree(thread_id: str) -> dict[str, object]:
    ws = await _ws(thread_id)
    return tree_view(ws.work, ws.baseline, ws.handle.id)


@app.get("/sandbox/{thread_id}/file")
async def file(thread_id: str, path: str = Query(...)) -> dict[str, object]:
    ws = await _ws(thread_id)
    return file_view(ws.work, ws.baseline, path)


class UsageIn(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0


class CostIn(BaseModel):
    calls: list[UsageIn]


@app.post("/studio/cost")
async def cost(body: CostIn) -> dict[str, object]:
    """Price provider-reported usage per call (the UI sends each AI message's usage)."""
    total, unknown = Decimal(0), 0
    for c in body.calls:
        priced = _prices.price(
            c.model,
            TokenUsage(
                c.input_tokens,
                c.output_tokens,
                c.cache_write_5m_tokens,
                c.cache_write_1h_tokens,
                c.cache_read_tokens,
            ),
        )
        if priced is None:
            unknown += 1
        else:
            total += priced.total
    return {
        "total_usd": f"{total:.6f}",
        "calls": len(body.calls),
        "unpriced_calls": unknown,
        "pricing_version": _prices.version,
    }
