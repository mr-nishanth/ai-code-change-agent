"""Public, minimal probes. Only up/down per dependency (no secrets, no driver errors).

Readiness is `DescribeTable` on the application table (the composition root's probe), bounded by
READINESS_TIMEOUT_SECONDS; a missing or inactive table, a timeout or any error is 503."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

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
    probe = request.app.state.readiness
    timeout: float = request.app.state.settings.readiness_timeout_seconds
    table_ok = False
    if probe is not None:
        try:
            async with asyncio.timeout(timeout):
                table_ok = bool(await probe())
        except Exception as exc:
            get_logger("health").warning("readiness_dynamodb_failed", error_type=type(exc).__name__)
    if not table_ok:
        response.status_code = 503
    return ReadyResponse(
        status="ready" if table_ok else "not_ready", dependencies={"dynamodb": table_ok}
    )
