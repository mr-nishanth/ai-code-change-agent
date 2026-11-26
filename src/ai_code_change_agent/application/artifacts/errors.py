"""Artifact errors. Messages are fixed codes: never paths, content or secrets."""


class ArtifactError(Exception):
    code = "artifact_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class ArtifactNotFoundError(ArtifactError):
    """Missing, or belonging to another request or attempt (indistinguishable on purpose)."""

    code = "artifact_not_found"


class ArtifactIntegrityError(ArtifactError):
    code = "artifact_hash_mismatch"


class ArtifactTooLargeError(ArtifactError):
    code = "artifact_too_large"


class ArtifactRedactionError(ArtifactError):
    code = "redaction_failed"


class ArtifactSecretError(ArtifactError):
    """A secret-looking value was found in an artifact that must not be altered."""

    code = "secret_detected"


class ArtifactStorageError(ArtifactError):
    code = "artifact_storage_error"
