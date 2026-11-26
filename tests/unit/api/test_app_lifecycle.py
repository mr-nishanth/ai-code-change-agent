from fastapi.testclient import TestClient

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app


def test_lifespan_creates_and_disposes_engine_when_url_set() -> None:
    cfg = Settings.model_validate({"database_url": "postgresql+psycopg://u:p@localhost:1/none"})
    app = create_app(cfg)
    with TestClient(app):
        assert app.state.engine is not None
