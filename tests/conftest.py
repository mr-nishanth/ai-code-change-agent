from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads .env from the CWD; never let tests pick up the developer's real file."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({"app_env": "test", "log_json": True})


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
