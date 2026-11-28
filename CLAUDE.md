# CLAUDE.md

Follow [AGENTS.md](AGENTS.md). Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/ARCHITECTURE-DECISIONS.md](docs/ARCHITECTURE-DECISIONS.md) and [docs/SECURITY.md](docs/SECURITY.md) before changing behaviour.

```bash
uv sync
uv run pytest                                             # needs Floci up and bootstrapped
uv run ruff check . && uv run ruff format --check .
uv run mypy
docker compose -f infra/docker-compose.yml up -d --wait   # Floci: DynamoDB, SQS, S3 on 127.0.0.1:4566
sh infra/floci/init/bootstrap.sh                          # tables, queue + DLQ, bucket (idempotent)
uv run uvicorn ai_code_change_agent.main:app --reload
uv run ai-code-change worker
```

Do not:
- read, print or modify secrets in `.env`;
- perform real GitHub or Jira writes or real model calls without explicit authorisation;
- add agent roles, Redis or a static repository allowlist;
- weaken sandbox hardening or tests.
