"""Fixtures for tests against the local Floci emulator (`infra/docker-compose.yml`).

Run with: uv run pytest -m floci -rs. When Floci is not running every test is a SKIP with a reason."""

import shutil
import subprocess

import pytest

from tests.support.dynamo import app_table, floci, job_queues  # noqa: F401 - fixtures


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/integration/floci/" in item.path.as_posix():
            item.add_marker(pytest.mark.floci)


@pytest.fixture(autouse=True)
def clean_db() -> None:
    """Overrides the integration suite's autouse fixture: Floci tests never touch PostgreSQL."""


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker argv in a test helper
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="session")
def docker_ready() -> None:
    """For tests whose Floci resources run as containers (Lambda, ECS, RDS)."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not found")
    if _docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        pytest.skip("docker daemon not reachable")
