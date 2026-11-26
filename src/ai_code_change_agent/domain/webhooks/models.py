"""Value objects, outcomes and typed errors for webhook ingestion."""

from dataclasses import dataclass
from enum import StrEnum


class ProcessingStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    IGNORED = "IGNORED"


class IgnoreReason(StrEnum):
    EVENT_NOT_SUPPORTED = "event_not_supported"
    PROJECT_OUT_OF_SCOPE = "project_out_of_scope"
    ISSUE_TYPE_OUT_OF_SCOPE = "issue_type_out_of_scope"


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"


class RejectionCode(StrEnum):
    CONTENT_TYPE_UNSUPPORTED = "content_type_unsupported"
    BODY_MISSING = "body_missing"
    SIGNATURE_MISSING = "signature_missing"
    SIGNATURE_MALFORMED = "signature_malformed"
    SIGNATURE_UNSUPPORTED_METHOD = "signature_unsupported_method"
    SIGNATURE_MISMATCH = "signature_mismatch"
    DELIVERY_ID_INVALID = "delivery_id_invalid"
    PAYLOAD_INVALID = "payload_invalid"


class WebhookRejected(Exception):  # noqa: N818 - domain term, not an error suffix
    """The request must be refused; carries only a machine-readable code."""

    def __init__(self, code: RejectionCode) -> None:
        super().__init__(code.value)
        self.code = code


class PersistenceError(Exception):
    """Storage failed. Message holds only an error class name (never driver text/DSN)."""


@dataclass(frozen=True)
class IssueRef:
    issue_id: str
    issue_key: str
    project_key: str
    issue_type: str


@dataclass(frozen=True)
class ReceivedDelivery:
    """A verified delivery, ready to be recorded. Never holds the payload itself."""

    delivery_id: str
    payload_sha256: str
    event_type: str
    retry_count: int
    flow: str | None
    status: ProcessingStatus
    ignore_reason: IgnoreReason | None
    issue: IssueRef | None


@dataclass(frozen=True)
class WebhookResult:
    outcome: Outcome
    delivery_id: str
    reason: str | None = None
