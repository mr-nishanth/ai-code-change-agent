"""FastAPI application: Jira webhook intake and health endpoints. The API never runs agents,
sandboxes or long workflows; accepted Stories become durable jobs for the worker (one DynamoDB
transaction; the worker's dispatcher sends them to SQS, the API never calls SQS)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_code_change_agent import __version__
from ai_code_change_agent.api.middleware.request_id import RequestIdMiddleware
from ai_code_change_agent.api.routes import health, jira_webhook
from ai_code_change_agent.composition import build_api
from ai_code_change_agent.core.config import Settings, get_settings
from ai_code_change_agent.core.logging import configure_logging, get_logger


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg.log_level, json_output=cfg.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log = get_logger("startup")
        api = build_api(cfg)
        app.state.webhook_service = api.webhook_service
        app.state.readiness = api.readiness
        log.info(
            "app_started",
            environment=cfg.app_env,
            dynamodb_table=cfg.dynamodb_table_name,
            webhook_enabled=app.state.webhook_service is not None,
        )
        try:
            yield
        finally:
            log.info("app_stopped")

    app = FastAPI(title="AI Code Change Agent", version=__version__, lifespan=lifespan)
    app.state.settings = cfg
    app.state.readiness = None
    app.state.webhook_service = None
    app.add_middleware(RequestIdMiddleware)
    app.include_router(health.router)
    app.include_router(jira_webhook.router)
    return app


def __getattr__(name: str) -> FastAPI:  # lazy: `uvicorn ai_code_change_agent.main:app`
    if name == "app":
        return create_app()
    raise AttributeError(name)
