from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app


class _Conn:
    async def execute(self, *_: Any) -> None:
        return None


class _Engine:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_Conn]:
        if self.fail:
            raise ConnectionError("postgresql+psycopg://u:hunter2@h/db refused")
        yield _Conn()


def test_live(client: TestClient) -> None:
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_ready_503_when_database_not_configured(client: TestClient) -> None:
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "not_ready", "dependencies": {"database": False}}


def test_ready_200_with_healthy_database(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.engine = _Engine()
        r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["dependencies"] == {"database": True}


def test_ready_503_and_no_leak_when_database_fails(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.engine = _Engine(fail=True)
        r = c.get("/health/ready")
    assert r.status_code == 503
    assert "hunter2" not in r.text
    assert "hunter2" not in capsys.readouterr().out


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    assert client.get("/health/live").headers["X-Request-ID"]
    assert (
        client.get("/health/live", headers={"X-Request-ID": "abc-1"}).headers["X-Request-ID"]
        == "abc-1"
    )


def test_unsafe_request_id_is_replaced(client: TestClient) -> None:
    r = client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert r.headers["X-Request-ID"] != "bad id\twith spaces"
