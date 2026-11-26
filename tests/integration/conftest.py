import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "postgresql+psycopg://acca:acca_dev_only@127.0.0.1:5435/ai_code_change_agent_test"


def libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": url, "APP_ENV": "test"}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)
    parsed = make_url(url)
    admin = parsed.set(database="postgres").render_as_string(hide_password=False)
    try:
        with psycopg.connect(libpq(admin), autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (parsed.database,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{parsed.database}"')
    except psycopg.OperationalError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"PostgreSQL not reachable for integration tests: {type(exc).__name__}")
    res = run_alembic(url, "upgrade", "head")
    assert res.returncode == 0, res.stderr[-2000:]
    return url


@pytest.fixture(autouse=True)
def clean_db(database_url: str) -> None:
    with psycopg.connect(libpq(database_url), autocommit=True) as conn:
        conn.execute(
            "TRUNCATE jira_deliveries, jobs, code_change_approvals, code_change_attempts, code_change_requests, repository_project_mappings, repository_aliases, repository_profile_versions, repositories, registry_audit_events CASCADE"
        )


def query(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    with psycopg.connect(libpq(url), autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()
