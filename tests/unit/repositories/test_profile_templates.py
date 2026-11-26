"""Standard profiles are generated for any repository key and pass the real profile validator."""

import pytest

from ai_code_change_agent.application.repositories.validation import ProfileValidator
from ai_code_change_agent.domain.repositories.templates import standard_profile


@pytest.mark.parametrize("key", ["shop-api", "billing-api", "any-new-repo"])
def test_the_standard_profile_is_valid_and_has_a_test_command(key: str) -> None:
    doc = standard_profile(key)
    validated = ProfileValidator().validate(doc)
    assert validated.profile.repository_key == key
    tests = [c for c in validated.profile.commands if c.kind.value == "test"]
    assert [tuple(c.argv) for c in tests] == [("python", "-m", "pytest", "-q")]


def test_the_test_command_can_be_overridden_and_unknown_toolchains_are_refused() -> None:
    doc = standard_profile("svc", test_argv=("python", "-m", "pytest", "-q", "tests"))
    assert ProfileValidator().validate(doc).profile.commands[0].argv[-1] == "tests"
    with pytest.raises(ValueError, match="toolchain"):
        standard_profile("svc", toolchain="cobol")


def test_a_profile_without_a_test_command_gets_the_toolchain_default_at_runtime() -> None:
    from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
    from ai_code_change_agent.domain.repositories.templates import with_default_test_command

    bare = standard_profile("new-repo") | {"commands": [], "capabilities": []}
    effective = with_default_test_command(RepositoryProfile.model_validate(bare))
    assert [c.argv for c in effective.commands] == [("python", "-m", "pytest", "-q")]
    assert "test_run" in effective.capabilities
    own = standard_profile("new-repo", test_argv=("python", "-m", "pytest", "tests"))
    kept = with_default_test_command(RepositoryProfile.model_validate(own))
    assert [c.argv for c in kept.commands] == [("python", "-m", "pytest", "tests")]  # own wins
    no_toolchain = RepositoryProfile.model_validate(bare | {"toolchains": []})
    assert with_default_test_command(no_toolchain).commands == ()
