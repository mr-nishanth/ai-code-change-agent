"""The delivery manifest: what the operator approves at the final gate (by its hash) and what the
delivery step re-verifies before any write to the hosted repository."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DeliveryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    issue_key: str
    request_id: str
    attempt_id: str
    attempt_number: int
    repository_id: str
    base_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changeset_ref: str
    changeset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_report_ref: str
    validation_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    branch: str
    commit_message: str
    commit_time: datetime  # fixed here so a retried delivery reproduces the same commit SHA
