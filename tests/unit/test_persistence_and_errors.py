import pytest

from ai_code_change_agent.core.errors import AppError, ConfigurationError


def test_error_hierarchy() -> None:
    assert issubclass(ConfigurationError, AppError)
    with pytest.raises(AppError):
        raise ConfigurationError("bad")
