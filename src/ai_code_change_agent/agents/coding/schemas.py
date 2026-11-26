"""Structured outputs of the coding agent. They are the agent's proposals and account, never the
authority: the plan is approved by a human, the result is judged by the validation engine."""

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ai_code_change_agent.application.workspaces.errors import UnsafePathError
from ai_code_change_agent.application.workspaces.paths import validate_relative_path

_Text = Field(min_length=1, max_length=2000)


class PlannedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=300)
    operation: Literal["create", "modify", "delete"]
    purpose: str = _Text

    @field_validator("path")
    @classmethod
    def _safe(cls, value: str) -> str:
        try:
            return validate_relative_path(value)
        except UnsafePathError as exc:
            msg = "path must be repository-relative without traversal"
            raise ValueError(msg) from exc


def _as_list(value: object) -> object:
    """Models sometimes send a list field as a JSON-encoded string or one plain string. Accept
    those shapes instead of letting the structured-output retry loop spin (observed with Haiku)."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
        lines = [ln.strip().lstrip("-*").strip() for ln in text.splitlines()]
        return [ln for ln in lines if ln] if len(lines) > 1 else ([text] if text else [])
    return value


StrList = Annotated[list[str], BeforeValidator(_as_list)]


def _requirements(value: object) -> object:
    """Drop a trailing human note such as "httpx==0.28.1 (already installed)" before the strict
    dependency policy runs; the requirement itself is still validated unchanged."""
    items = _as_list(value)
    if isinstance(items, list):
        return [
            re.sub(r"\s*\([^)]*\)\s*$", "", i).strip() if isinstance(i, str) else i for i in items
        ]
    return items


class ImplementationPlan(BaseModel):
    """Output of the planning phase (read-only tools). Approved by its hash, by the automatic policy
    gate or, in manual mode, by an operator."""

    model_config = ConfigDict(extra="forbid")

    summary: str = _Text
    acceptance_criteria: StrList = Field(default_factory=list, max_length=50)
    findings: StrList = Field(default_factory=list, max_length=50)
    changes: Annotated[list[PlannedChange], BeforeValidator(_as_list)] = Field(
        min_length=1, max_length=100
    )
    tests: StrList = Field(default_factory=list, max_length=50)
    risks: StrList = Field(default_factory=list, max_length=50)
    todo: StrList = Field(default_factory=list, max_length=100)
    # Python packages the implementation needs that the sandbox lacks: a PEP 508 name with
    # optional version specifiers only (no URLs, paths or markers). Validated and prepared by the
    # platform (ADR-010); the plan hash covers this list.
    dependencies: Annotated[list[str], BeforeValidator(_requirements)] = Field(
        default_factory=list, max_length=30
    )


class ImplementationReport(BaseModel):
    """The agent's own account of the implementation phase. Informational only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["implemented", "partially_implemented", "blocked"]
    summary: str = _Text
    changed_files: StrList = Field(default_factory=list, max_length=200)
    tests_added_or_updated: StrList = Field(default_factory=list, max_length=100)
    commands_run: StrList = Field(default_factory=list, max_length=100)
    notes: StrList = Field(default_factory=list, max_length=50)
    # Multi-repository Stories: the interface dependent repositories must follow (validated code
    # stays authoritative; this text becomes an immutable contract artifact for them).
    published_contract: str | None = Field(default=None, max_length=20_000)
    # Documentation consulted (URLs with retrieval dates), when research tools were used.
    references: StrList = Field(default_factory=list, max_length=30)
