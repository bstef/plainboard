"""Where semantic search exists: PostgreSQL whose server offers pgvector. Elsewhere (SQLite, a server
without the extension) its tables are never made and the feature is absent."""

from __future__ import annotations

import logging
import time

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_config
from ...models import Base

log = logging.getLogger(__name__)


def vector_tables() -> list:
    return [t for t in Base.metadata.sorted_tables if t.info.get("needs") == "vector"]


def ensure(conn: Connection) -> bool:
    """Make the extension and the search tables where they can be: from the migration that brought them,
    and at every start, so a server that offers pgvector later (another image) gets them then. Idempotent."""
    if conn.dialect.name != "postgresql":
        return False
    if not conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first():
        if not conn.execute(text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")).first():
            log.info("PostgreSQL offers no pgvector: semantic search is not available")
            return False
        try:
            with conn.begin_nested():  # a refusal (no right to create it) leaves the migration's transaction usable
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except DBAPIError as exc:
            log.warning("Could not create the extension vector, semantic search is not available: %s", exc)
            return False
    Base.metadata.create_all(conn, tables=vector_tables(), checkfirst=True)
    return True


_known: dict[str, tuple[bool, float]] = {}
RECHECK_SECONDS = 300  # absent: looked at again after this (the tables come with the next start)


async def available(session: AsyncSession) -> bool:
    """Whether this installation has semantic search: PostgreSQL with the search tables."""
    config = get_config()
    if not config.postgres:
        return False
    url = config.database
    known = _known.get(url)
    if known and (known[0] or time.monotonic() - known[1] < RECHECK_SECONDS):
        return known[0]
    there = bool(await session.scalar(text("SELECT to_regclass('search_documents') IS NOT NULL")))
    _known[url] = (there, time.monotonic())
    return there
