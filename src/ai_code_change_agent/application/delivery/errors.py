"""Delivery errors: fixed codes only (never paths, content, command output or secrets)."""

from ai_code_change_agent.application.errors import WorkflowError


class DeliveryError(WorkflowError):
    code = "delivery_error"


# Failures where a remote write may have happened: never retried automatically; the next delivery
# call reconciles remote state (lookups before any write).
UNKNOWN_OUTCOME_CODE = "delivery_outcome_unknown"
