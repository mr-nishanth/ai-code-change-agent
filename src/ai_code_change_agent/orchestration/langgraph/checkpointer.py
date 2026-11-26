"""LangGraph checkpoints in PostgreSQL, in their own schema (logically separate from the domain
tables). The coding agent's runs checkpoint under their own thread ids in the same saver."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier


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
