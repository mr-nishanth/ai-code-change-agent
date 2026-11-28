from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app

_AMBIENT_AWS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ACCA_BEDROCK_ENDPOINT_URL",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "ACCA_AWS_REGION",
    "ACCA_DYNAMODB_TABLE_NAME",
    "ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME",
    "ACCA_SQS_JOB_QUEUE_NAME",
    "ACCA_S3_BUCKET_NAME",
)
# development/test Settings require a local AWS endpoint; tests never reach anything else.
LOCAL_AWS_ENDPOINT = "http://127.0.0.1:4566"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings reads .env from the CWD; never let tests pick up the developer's real file.
    AWS variables from the shell (for example the dummy Floci credentials) are cleared too, so
    tests that need them set them explicitly and the outcome never depends on the shell."""
    monkeypatch.chdir(tmp_path)
    for name in _AMBIENT_AWS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", LOCAL_AWS_ENDPOINT)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({"app_env": "test", "log_json": True})


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
