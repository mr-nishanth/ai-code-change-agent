"""The project stores the studio reads (dev only, read-only use), built once from its Settings.

Everything goes through the application's ports, never raw queries: attempts, requests and the agent
thread index through `CodeChangeService` (DynamoDB change store), repositories through the registry
unit of work, costs through `CostQuery`, the PR link through `ArtifactStore` (S3) and agent state
through the DynamoDB checkpointer (`aget_state` only: no setup, no writes)."""

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver

from ai_code_change_agent.application.artifacts.ports import ArtifactStore
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.costs.report import CostQuery
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.execution.s3_artifacts import S3ArtifactStore
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.orchestration.langgraph.checkpointer import dynamodb_checkpointer
from ai_code_change_agent.persistence.dynamodb import Table
from ai_code_change_agent.persistence.dynamodb.change_store import DynamoChangeUnitOfWork
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore
from studio.workspaces import settings


@dataclass(frozen=True)
class StudioStores:
    changes: CodeChangeService
    registry: Callable[[], RegistryUnitOfWork]
    costs: CostQuery
    artifacts: ArtifactStore
    checkpointer: BaseCheckpointSaver[str]


def build_stores(s: Settings) -> StudioStores:
    aws = AwsClients(s)
    table = Table(aws.dynamodb, s.dynamodb_table_name)
    return StudioStores(
        changes=CodeChangeService(lambda: DynamoChangeUnitOfWork(table)),
        registry=lambda: DynamoRegistryUnitOfWork(table),
        costs=DynamoUsageStore(table),
        artifacts=S3ArtifactStore(
            aws.s3, s.s3_bucket_name, max_artifact_bytes=s.artifact_max_bytes
        ),
        checkpointer=dynamodb_checkpointer(aws, s),
    )


_stores: StudioStores | None = None


def stores() -> StudioStores:
    global _stores
    if _stores is None:
        _stores = build_stores(settings())
    return _stores
