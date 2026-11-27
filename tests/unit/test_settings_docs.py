"""The documented environment contract covers every ACCA_ setting.

`core/config.py` reads the AWS settings under `ACCA_` aliases; each must be listed in `.env.example`
(as an assignment) and in `docs/CONFIGURATION.md` (as a table row), and the removed database settings
must not come back.
"""

import re
from pathlib import Path

from ai_code_change_agent.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
CONFIGURATION = (ROOT / "docs" / "CONFIGURATION.md").read_text()


def acca_settings() -> set[str]:
    return {
        alias
        for field in Settings.model_fields.values()
        if isinstance(alias := field.validation_alias, str) and alias.startswith("ACCA_")
    }


def test_the_acca_settings_are_found() -> None:
    names = acca_settings()
    assert {"ACCA_AWS_REGION", "ACCA_AWS_ENDPOINT_URL", "ACCA_BEDROCK_ENDPOINT_URL"} <= names
    assert len(names) >= 9, sorted(names)


def test_every_acca_setting_is_in_env_example() -> None:
    missing = [
        name
        for name in sorted(acca_settings())
        if not re.search(rf"^{name}=", ENV_EXAMPLE, flags=re.MULTILINE)
    ]
    assert not missing, missing


def test_every_acca_setting_is_in_the_configuration_doc() -> None:
    missing = [
        name
        for name in sorted(acca_settings())
        if not re.search(rf"^\|[^\n]*\b{name}\b[^\n]*\|", CONFIGURATION, flags=re.MULTILINE)
    ]
    assert not missing, missing


def test_the_removed_database_settings_stay_removed() -> None:
    for text in (ENV_EXAMPLE, CONFIGURATION):
        for removed in ("DATABASE_URL", "LANGGRAPH_CHECKPOINT_SCHEMA", "alembic", "psycopg"):
            assert removed not in text, removed
