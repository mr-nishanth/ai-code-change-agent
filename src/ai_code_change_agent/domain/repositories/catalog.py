"""Platform-owned allow-lists. Repositories select from these; they cannot extend them."""

CAPABILITIES = frozenset(
    {"test_run", "build_run", "lint_run", "format_check", "dependency_install"}
)
COMMAND_POLICY_IDS = frozenset({"default-v1"})
VALIDATION_POLICIES = frozenset({"standard", "strict"})
EXECUTION_TIERS = ("small", "medium", "large")
ACCESS_REFERENCE_SCHEMES = ("app-installation", "secret", "parameter")

# Platform ceilings a profile can only lower.
MAX_FILES_CHANGED_CEILING = 500
MAX_DIFF_BYTES_CEILING = 5_242_880
MAX_RUNTIME_SECONDS_CEILING = 14_400
MAX_COMMAND_TIMEOUT_CEILING = 3_600
MAX_INDEX_FILE_BYTES_CEILING = 5_242_880
MAX_PROFILE_BYTES = 262_144
