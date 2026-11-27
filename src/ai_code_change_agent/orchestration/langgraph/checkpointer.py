"""LangGraph checkpoints. The coding agent's runs checkpoint under their own thread ids in the same
saver as the parent workflow.

`dynamodb_checkpointer` is the DynamoDB saver (`langgraph-checkpoint-aws`, DM 9.4): the checkpoint
table, strongly consistent reads (a resume job may run on another worker right after the checkpoint
was written) and mandatory S3 offload for payloads over the library's 358,400-byte threshold, under
the `checkpoints/` key prefix of the application bucket."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph_checkpoint_aws.checkpoint.dynamodb import (  # type: ignore[import-untyped]
    DynamoDBSaver,
)

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.integrations.aws.clients import client_config

SECONDS_PER_DAY = 86_400
CHECKPOINT_S3_PREFIX = "checkpoints/"


def dynamodb_checkpointer(clients: AwsClients, settings: Settings) -> BaseCheckpointSaver[str]:
    """The DynamoDB saver on the application's AWS session and client config.

    The S3 client gets an explicit endpoint only with the local override (Floci); otherwise it
    resolves real S3 for the region like every other client. Retention is
    `ACCA_CHECKPOINT_TTL_DAYS` (default 30): the saver writes it as the items' `ttl` attribute and
    installs the matching S3 lifecycle rule for offloaded payloads (whole days, DM 9.4)."""
    s3_offload: dict[str, str] = {
        "bucket_name": settings.s3_bucket_name,
        "key_prefix": CHECKPOINT_S3_PREFIX,
    }
    if clients.endpoint_url is not None:
        s3_offload["endpoint_url"] = clients.endpoint_url
    saver: BaseCheckpointSaver[str] = DynamoDBSaver(
        table_name=settings.dynamodb_checkpoint_table_name,
        session=clients.session,
        region_name=clients.region,
        endpoint_url=clients.endpoint_url,
        boto_config=client_config(),
        ttl_seconds=checkpoint_ttl_seconds(settings),
        s3_offload_config=s3_offload,
        consistent_read=True,
    )
    return saver


def checkpoint_ttl_seconds(settings: Settings) -> int:
    return settings.checkpoint_ttl_days * SECONDS_PER_DAY
