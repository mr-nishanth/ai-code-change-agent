"""Public, minimal probes. Only up/down per dependency (no secrets, no driver errors)."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from ai_code_change_agent import __version__
from ai_code_change_agent.core.logging import get_logger

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["alive"] = "alive"
    version: str = __version__


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    dependencies: dict[str, bool]


@router.get("/live")
async def live() -> LiveResponse:
    return LiveResponse()


@router.get("/ready")
async def ready(request: Request, response: Response) -> ReadyResponse:
    engine = request.app.state.engine
    timeout: float = request.app.state.settings.readiness_timeout_seconds
    database_ok = False
    if engine is not None:
        try:
            async with asyncio.timeout(timeout), engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            database_ok = True
        except Exception as exc:
            get_logger("health").warning("readiness_db_failed", error_type=type(exc).__name__)
    if not database_ok:
        response.status_code = 503
    return ReadyResponse(
        status="ready" if database_ok else "not_ready", dependencies={"database": database_ok}
    )
