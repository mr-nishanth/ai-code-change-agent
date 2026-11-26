"""Validation of RepositoryProfile documents against the schema and the platform policy."""

import json
import tomllib
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from ai_code_change_agent.domain.repositories.catalog import MAX_PROFILE_BYTES
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.errors import Finding, RegistryValidationError
from ai_code_change_agent.domain.repositories.profile import (
    PROFILE_SCHEMA_VERSION,
    RepositoryProfile,
    canonical_hash,
    profile_findings,
)


@dataclass(frozen=True)
class ValidatedProfile:
    profile: RepositoryProfile
    content: dict[str, Any]
    content_sha256: str
    schema_version: int
    policy_version: str


def parse_document(raw: bytes, fmt: Literal["json", "toml"]) -> Any:
    """Parse an untrusted profile document. Errors never echo the input."""
    if len(raw) > MAX_PROFILE_BYTES:
        raise RegistryValidationError(
            [Finding("document", "too_large", "profile document is too large")]
        )
    try:
        if fmt == "json":
            return json.loads(raw)
        return tomllib.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise RegistryValidationError(
            [Finding("document", "unparseable", f"not valid {fmt}")]
        ) from None


class ProfileValidator:
    def __init__(self, policy: CommandPolicy | None = None) -> None:
        self.policy = policy or CommandPolicy()

    def validate(self, document: object) -> ValidatedProfile:
        if not isinstance(document, dict):
            raise RegistryValidationError(
                [Finding("document", "not_an_object", "expected an object")]
            )
        try:
            profile = RepositoryProfile.model_validate(document)
        except ValidationError as exc:
            raise RegistryValidationError(
                [
                    Finding(".".join(str(p) for p in e["loc"]) or "document", e["type"], e["msg"])
                    for e in exc.errors(include_input=False, include_url=False)
                ]
            ) from None
        findings = profile_findings(profile, self.policy)
        if findings:
            raise RegistryValidationError(findings)
        content = profile.model_dump(mode="json")
        return ValidatedProfile(
            profile=profile,
            content=content,
            content_sha256=canonical_hash(content),
            schema_version=PROFILE_SCHEMA_VERSION,
            policy_version=self.policy.version,
        )
