"""Profile documents for Python repository stacks (repository execution is Python only). Pure data: no repository-specific code exists."""

from typing import Any

from ai_code_change_agent.domain.repositories.models import NewRepository


def _cmd(cid: str, kind: str, toolchain: str, *argv: str, network: str = "none") -> dict[str, Any]:
    return {"id": cid, "kind": kind, "toolchain": toolchain, "argv": list(argv), "network": network}


def profile(key: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "repository_key": key,
        "command_policy": "default-v1",
        "validation_policy": "standard",
    }
    base.update(over)
    return base


def django_service() -> dict[str, Any]:
    return profile(
        "django-service",
        languages=["Python"],
        frameworks=["Django"],
        toolchains=["python"],
        capabilities=["test_run", "lint_run"],
        commands=[
            _cmd("test", "test", "python", "python", "-m", "pytest", "-q"),
            _cmd("lint", "lint", "python", "ruff", "check", "--no-fix", "src"),
        ],
        execution_tier="medium",
    )


def flask_worker() -> dict[str, Any]:
    return profile(
        "flask-worker",
        languages=["Python"],
        frameworks=["Flask"],
        toolchains=["python"],
        capabilities=["test_run", "lint_run", "format_check"],
        commands=[
            _cmd("test", "test", "python", "pytest", "-q", "tests"),
            _cmd("types", "lint", "python", "mypy", "src"),
            _cmd("fmt", "format_check", "python", "ruff", "format", "--check", "src"),
        ],
    )


def python_backend() -> dict[str, Any]:
    return profile(
        "python-backend",
        languages=["Python"],
        frameworks=["FastAPI"],
        toolchains=["python"],
        capabilities=["test_run", "lint_run", "format_check"],
        commands=[
            _cmd("test", "test", "python", "pytest", "-q", "tests"),
            _cmd("lint", "lint", "python", "ruff", "check", "src"),
            _cmd("fmt", "format_check", "python", "ruff", "format", "--check", "src"),
            _cmd("types", "lint", "python", "mypy", "--strict", "src"),
        ],
        limits={"max_files_changed": 30},
    )


def unfamiliar_framework() -> dict[str, Any]:
    """A stack with no execution toolchain: valid, but declares no commands."""
    return profile(
        "odd-framework",
        languages=["Zig"],
        frameworks=["Zigweb"],
        validation_policy="strict",
    )


def python_sdk() -> dict[str, Any]:
    return profile(
        "python-sdk",
        languages=["Python"],
        frameworks=[],
        toolchains=["python"],
        capabilities=["test_run"],
        commands=[_cmd("test", "test", "python", "python", "-m", "pytest", "-q", "tests")],
    )


FIXTURES: dict[str, Any] = {
    "django-service": django_service,
    "flask-worker": flask_worker,
    "python-backend": python_backend,
    "odd-framework": unfamiliar_framework,
}


def new_repo(
    key: str,
    *,
    provider: str = "github",
    pid: str | None = None,
    owner: str = "acme",
    name: str | None = None,
    ref: str = "app-installation:12345",
) -> NewRepository:
    return NewRepository(
        repository_key=key, display_name=key.replace("-", " ").title(), git_provider=provider,
        provider_repository_id=pid or f"id-{key}", owner=owner, name=name or key,
        default_branch="main", access_reference=ref,
    )  # fmt: skip
