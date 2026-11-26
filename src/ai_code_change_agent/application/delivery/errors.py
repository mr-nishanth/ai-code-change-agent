"""Delivery errors: fixed codes only (never paths, content, command output or secrets)."""

from ai_code_change_agent.application.errors import WorkflowError


class DeliveryError(WorkflowError):
    code = "delivery_error"


# Failure codes that mean an integrity or identity violation: the attempt is failed (terminal), the
# approval is never reused. Every other code leaves the attempt where it is so a retry is possible.
INTEGRITY_CODES: frozenset[str] = frozenset(
    {
        "manifest_hash_mismatch",
        "manifest_invalid",
        "manifest_verification_failed",
        "gate_c_result_conflict",
        "artifact_hash_mismatch",
        "artifact_missing",
        "artifact_schema_invalid",
        "approval_invalid",
        "delivery_tree_mismatch",
        "baseline_mismatch",
        "target_identity_invalid",
        "unsafe_repository_config",
        "unsafe_path",
        "protected_path",
        "delivery_receipt_invalid",
        "base_commit_missing",  # Gate D.1: the approved base commit is not in the remote
    }
)
# Conflicts are reported, never overwritten and never fail the attempt.
CONFLICT_CODES: frozenset[str] = frozenset(
    {"manifest_conflict", "branch_conflict", "pull_request_conflict"}
)
# Failures where a remote write may have happened: never retried automatically; the next delivery
# call reconciles remote state (lookups before any write).
UNKNOWN_OUTCOME_CODE = "delivery_outcome_unknown"
