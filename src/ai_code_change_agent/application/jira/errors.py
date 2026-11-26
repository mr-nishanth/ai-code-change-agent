"""Stable, safe Jira integration errors.

Messages are fixed reason codes chosen by this codebase. Raw Jira response bodies, headers,
credentials and connection details are never placed in an exception.
"""

from ai_code_change_agent.core.errors import AppError


class JiraIntegrationError(AppError):
    code = "jira_error"
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        operation: str = "unknown",
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message or self.code)
        self.operation = operation
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable


class JiraAuthenticationError(JiraIntegrationError):
    code = "jira_authentication_failed"


class JiraPermissionError(JiraIntegrationError):
    code = "jira_permission_denied"


class JiraIssueNotFoundError(JiraIntegrationError):
    """Jira answers 404 for both missing and inaccessible issues; they cannot be told apart."""

    code = "jira_issue_not_found"


class JiraInvalidIssueReferenceError(JiraIntegrationError):
    code = "jira_invalid_issue_reference"


class JiraRateLimitError(JiraIntegrationError):
    code = "jira_rate_limited"
    retryable = True

    def __init__(
        self,
        message: str | None = None,
        *,
        operation: str = "unknown",
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, operation=operation, status_code=status_code)
        self.retry_after_seconds = retry_after_seconds


class JiraServiceUnavailableError(JiraIntegrationError):
    code = "jira_service_unavailable"
    retryable = True


class JiraTimeoutError(JiraIntegrationError):
    code = "jira_timeout"
    retryable = True


class JiraResponseError(JiraIntegrationError):
    """Unexpected status, redirect, size, content type or shape."""

    code = "jira_response_invalid"
