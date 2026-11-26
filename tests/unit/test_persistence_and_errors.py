import pytest

from ai_code_change_agent.core.errors import AppError, ConfigurationError
from ai_code_change_agent.persistence.models import Base
from ai_code_change_agent.persistence.session import create_engine, create_session_factory


def test_error_hierarchy() -> None:
    assert issubclass(ConfigurationError, AppError)
    with pytest.raises(AppError):
        raise ConfigurationError("bad")


def test_base_metadata_has_naming_convention() -> None:
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


async def test_engine_and_session_factory_are_lazy() -> None:
    engine = create_engine("postgresql+psycopg://u:p@localhost:1/none")  # no connection made
    factory = create_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False
    await engine.dispose()
