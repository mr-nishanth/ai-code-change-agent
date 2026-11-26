"""Profile documents for four technology stacks. Pure data: no repository-specific code exists."""

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


def nestjs_backend() -> dict[str, Any]:
    return profile(
        "nest-backend",
        languages=["TypeScript"],
        frameworks=["NestJS"],
        toolchains=["node"],
        capabilities=["test_run", "build_run", "lint_run", "dependency_install"],
        commands=[
            _cmd(
                "install",
                "install",
                "node",
                "npm",
                "ci",
                "--ignore-scripts",
                network="package_registry",
            ),
            _cmd("test", "test", "node", "npm", "run", "test"),
            _cmd("lint", "lint", "node", "npm", "run", "lint"),
            _cmd("build", "build", "node", "npm", "run", "build"),
        ],
        execution_tier="medium",
    )


def react_frontend() -> dict[str, Any]:
    return profile(
        "react-frontend",
        languages=["TypeScript", "JavaScript"],
        frameworks=["React", "Vite"],
        toolchains=["node"],
        capabilities=["test_run", "build_run", "lint_run", "format_check"],
        commands=[
            _cmd("test", "test", "node", "npm", "run", "test"),
            _cmd("typecheck", "lint", "node", "npm", "run", "typecheck"),
            _cmd("fmt", "format_check", "node", "npm", "run", "format:check"),
            _cmd("build", "build", "node", "npm", "run", "build"),
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
    """A stack the platform has no toolchain policy for: valid, but declares no commands."""
    return profile(
        "odd-framework",
        languages=["Zig"],
        frameworks=["Zigweb"],
        validation_policy="strict",
    )


def jvm_service() -> dict[str, Any]:
    return profile(
        "spring-service",
        languages=["Java"],
        frameworks=["SpringBoot"],
        toolchains=["jvm"],
        capabilities=["test_run", "build_run"],
        commands=[
            _cmd("test", "test", "jvm", "mvn", "test", "-B"),
            _cmd("build", "build", "jvm", "mvn", "compile", "-B"),
        ],
    )


FIXTURES: dict[str, Any] = {
    "nest-backend": nestjs_backend,
    "react-frontend": react_frontend,
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
