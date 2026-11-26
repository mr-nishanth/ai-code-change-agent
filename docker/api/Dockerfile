# API image: FastAPI webhook intake and health endpoints only. It never starts sandboxes and has
# no Docker access. Built from the locked dependency set with uv.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --system --uid 10001 --no-create-home app
USER 10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn ai_code_change_agent.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000} --proxy-headers"]
