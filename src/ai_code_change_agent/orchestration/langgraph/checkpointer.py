"""LangGraph checkpoints. The coding agent's runs checkpoint under their own thread ids in the same
saver as the parent workflow.

`dynamodb_checkpointer` is the DynamoDB saver (`langgraph-checkpoint-aws`, DM 9.4): the checkpoint
table, strongly consistent reads (a resume job may run on another worker right after the checkpoint
was written) and mandatory S3 offload for payloads over the library's 358,400-byte threshold, under
the `checkpoints/` key prefix of the application bucket. `postgres_checkpointer` (checkpoints in
their own PostgreSQL schema) is the current saver until the composition switches."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph_checkpoint_aws.checkpoint.dynamodb import (  # type: ignore[import-untyped]
    DynamoDBSaver,
)
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.integrations.aws.clients import client_config

CHECKPOINT_TTL_SECONDS = 7_776_000  # 90 days: DynamoDB TTL and an S3 lifecycle rule (DM 9.4)
CHECKPOINT_S3_PREFIX = "checkpoints/"


def dynamodb_checkpointer(clients: AwsClients, settings: Settings) -> BaseCheckpointSaver[str]:
    """The DynamoDB saver on the application's AWS session and client config.

    The S3 client gets an explicit endpoint only with the local override (Floci); otherwise it
    resolves real S3 for the region like every other client."""
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
        ttl_seconds=CHECKPOINT_TTL_SECONDS,
        s3_offload_config=s3_offload,
        consistent_read=True,
    )
    return saver


def libpq_url(database_url: str) -> str:
    """SQLAlchemy URL (postgresql+psycopg://) to a libpq conninfo URL."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_checkpointer(
    database_url: str, schema: str
) -> AsyncIterator[AsyncPostgresSaver]:
    url = libpq_url(database_url)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
        await admin.execute(SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(schema)))
    conn = await psycopg.AsyncConnection.connect(
        url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={schema}",
    )
    try:
        saver = AsyncPostgresSaver(conn)
        await saver.setup()
        yield saver
    finally:
        await conn.close()
