# AI Code Change Agent

Turns a Jira Story into **draft pull requests**, for one repository or several. One LangGraph workflow, one Deep Agent and one independent Python validation engine. The coding agent works in an offline, hardened Docker sandbox, every change is validated independently, and nothing is merged automatically.

**Status (2026-11-26):** verified end to end with claude-haiku-5-5:
- KAN-7 and KAN-8: single repository;
- KAN-9: backend and client repositories, with the backend's API contract handed to the client.

Python and Node toolchains are supported. Android, Flutter and iOS are not yet.

## Start here

| Doc | What |
|---|---|
| [docs/PROJECT-FLOW.md](docs/PROJECT-FLOW.md) | How the whole flow works, Story to PR |
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | Setup and run guide |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/ARCHITECTURE-DECISIONS.md](docs/ARCHITECTURE-DECISIONS.md) | Architecture and ADRs |
| [docs/SECURITY.md](docs/SECURITY.md) | Security model |
| [docs/AUTOMATED-PIPELINE.md](docs/AUTOMATED-PIPELINE.md) | Automatic gates, retries, onboarding |
| [docs/MULTI-REPOSITORY.md](docs/MULTI-REPOSITORY.md) | Stories across several repositories |
| [docs/TOOLCHAINS-AND-RESEARCH.md](docs/TOOLCHAINS-AND-RESEARCH.md) | Execution images, documentation search |
| [docs/DYNAMIC-DEPENDENCY-PROVISIONING.md](docs/DYNAMIC-DEPENDENCY-PROVISIONING.md) | Offline dependency bundles |
| [docs/LLM-COST-TRACKING.md](docs/LLM-COST-TRACKING.md) | Cost ledger, reports, runaway-cost guards |
| [docs/DEEP-AGENT-STUDIO.md](docs/DEEP-AGENT-STUDIO.md) | React UI to watch the agent live |
| [docs/SETUP-GITHUB-APP.md](docs/SETUP-GITHUB-APP.md) · [docs/SETUP-JIRA-WEBHOOK.md](docs/SETUP-JIRA-WEBHOOK.md) · [docs/EXTERNAL-INTEGRATIONS.md](docs/EXTERNAL-INTEGRATIONS.md) | External setup |
| [docs/history/](docs/history/) | Earlier reports (POC, readiness, migration) |

## Quick start

```bash
uv sync --locked && cp .env.example .env            # fill in .env
docker compose up -d --wait postgres && uv run alembic upgrade head
docker build -t ai-code-change-agent/execution-python:0.2.0 docker/execution
docker compose up -d --wait api && ngrok http 8000  # point the Jira webhook here
uv run ai-code-change repositories register --from-provider <owner>/<repo> --project <KEY> --enable
uv run ai-code-change worker
```
