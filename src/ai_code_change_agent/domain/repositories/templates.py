"""Standard Python repository profiles, for any repository key.

Operators onboard a repository with `ai-code-change repositories init-profile <key>` instead of
hand-writing a profile. The result is an ordinary profile document: it is still validated by the
ProfileValidator and the platform command policy, stored as an immutable version and auditable."""

from typing import Any

from ai_code_change_agent.domain.repositories.profile import CommandSpec, RepositoryProfile

# The platform's standard test command per toolchain (must be allowed by CommandPolicy).
DEFAULT_TEST_COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("python", "-m", "pytest", "-q"),
}
DEFAULT_LANGUAGES: dict[str, str] = {"python": "Python"}


def standard_profile(
    repository_key: str,
    toolchain: str = "python",
    test_argv: tuple[str, ...] | None = None,
    *,
    max_files_changed: int = 50,
    test_timeout_seconds: int = 300,
) -> dict[str, Any]:
    if toolchain not in DEFAULT_TEST_COMMANDS:
        msg = f"no standard profile for toolchain '{toolchain}'"
        raise ValueError(msg)
    argv = list(test_argv or DEFAULT_TEST_COMMANDS[toolchain])
    return {
        "schema_version": 1,
        "repository_key": repository_key,
        "languages": [DEFAULT_LANGUAGES[toolchain]],
        "frameworks": [],
        "toolchains": [toolchain],
        "capabilities": ["test_run"],
        "command_policy": "default-v1",
        "execution_tier": "small",
        "validation_policy": "standard",
        "indexing": {"include": [], "exclude": [], "max_file_bytes": 1048576},
        "limits": {
            "max_files_changed": max_files_changed,
            "max_diff_bytes": 1048576,
            "max_runtime_seconds": 3600,
        },
        "commands": [
            {
                "id": "test",
                "kind": "test",
                "toolchain": toolchain,
                "argv": argv,
                "cwd": ".",
                "env": {},
                "network": "none",
                "timeout_seconds": test_timeout_seconds,
            }
        ],
    }


def with_default_test_command(profile: RepositoryProfile) -> RepositoryProfile:
    """The effective profile: a profile whose toolchain has a standard test command but which
    defines no test command of its own gets that standard command (and the test_run capability).
    This keeps every registered repository working without per-repository configuration; a test
    command in the profile always wins."""
    if any(c.kind.value == "test" for c in profile.commands):
        return profile
    toolchain = next((t for t in profile.toolchains if t in DEFAULT_TEST_COMMANDS), None)
    if toolchain is None:
        return profile
    spec = standard_profile(profile.repository_key, toolchain)["commands"][0]
    capabilities = tuple(dict.fromkeys((*profile.capabilities, "test_run")))
    return profile.model_copy(
        update={
            "commands": (*profile.commands, CommandSpec.model_validate(spec)),
            "capabilities": capabilities,
        }
    )
