"""Typed registry errors. Messages are fixed codes; never include secrets or raw input."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """One validation problem. `field` is a path such as `commands.0.argv`."""

    field: str
    code: str
    message: str


class RegistryError(Exception):
    code = "registry_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class RegistryValidationError(RegistryError):
    code = "validation_failed"

    def __init__(self, findings: list[Finding]) -> None:
        super().__init__(f"{len(findings)} validation finding(s)")
        self.findings = tuple(findings)


class NotFoundError(RegistryError):
    code = "not_found"


class ConflictError(RegistryError):
    code = "conflict"


class InvalidStateError(RegistryError):
    code = "invalid_state"


class ProviderMismatchError(RegistryError):
    code = "provider_mismatch"
