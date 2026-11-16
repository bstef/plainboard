"""Read the database as it is, for whoever analyses how the agents work.

One SELECT at a time on a connection that cannot write, with SQLite's authorizer deciding what a
statement may touch: reading rows and calling functions, nothing else. Columns that hold
credentials (encrypted values, hashes, the settings' values) read as NULL whatever the statement
looks like, and a login inside a URL is blanked like in the work log. A statement that runs too
long is stopped. This is how questions get answered that no endpoint foresaw (counts per kind,
durations, who asked whom), without a new endpoint for each.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from ..config import get_config
from .errors import ServiceError
from .trace import URL_LOGIN

MAX_ROWS = 5000
SECONDS = 20.0
ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def hidden(table: str | None, column: str | None) -> bool:
    column = column or ""
    return column.endswith(("_enc", "_hash")) or (table == "settings" and column == "value")


def _authorize(action: int, first: str | None, second: str | None, _db: str | None, _source: str | None) -> int:
    if action not in ALLOWED:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and hidden(first, second):
        return sqlite3.SQLITE_IGNORE  # reads as NULL
    if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{get_config().data_dir / 'plainboard.db'}?mode=ro", uri=True, timeout=5)
    conn.set_authorizer(_authorize)
    deadline = time.monotonic() + SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 20_000)
    return conn


def _cell(value: Any, cell_chars: int) -> Any:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        value = URL_LOGIN.sub(r"\1[login]@", value)
        if cell_chars and len(value) > cell_chars:
            return value[:cell_chars] + f"... [{len(value)} chars]"
    return value


def _query(sql: str, limit: int, cell_chars: int) -> dict[str, Any]:
    conn = _connect()
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        rows = cursor.fetchmany(limit + 1)
    except sqlite3.Warning as exc:  # more than one statement
        raise ServiceError(f"One statement at a time: {exc}", 400) from exc
    except sqlite3.Error as exc:
        text = str(exc)
        why = "it ran longer than allowed" if "interrupted" in text else "only reading is allowed" if "not authorized" in text or "prohibited" in text else text
        raise ServiceError(f"Query refused: {why}", 400) from exc
    finally:
        conn.close()
    return {"columns": columns, "rows": [[_cell(v, cell_chars) for v in row] for row in rows[:limit]], "more": len(rows) > limit}


def _schema() -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{get_config().data_dir / 'plainboard.db'}?mode=ro", uri=True, timeout=5)
    try:
        out = []
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            columns = [f"{name} {kind}".strip() for _cid, name, kind, *_ in conn.execute(f'PRAGMA table_info("{table}")') if not hidden(table, name)]
            out.append({"table": table, "rows": conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0], "columns": columns})
        return out
    finally:
        conn.close()


async def query(sql: str, limit: int = 200, cell_chars: int = 2000) -> dict[str, Any]:
    """Rows of one SELECT. ``cell_chars``: longer texts are cut (0: never), so a look at many rows stays small."""
    return await asyncio.to_thread(_query, sql, max(1, min(limit, MAX_ROWS)), max(cell_chars, 0))


async def schema() -> list[dict[str, Any]]:
    """Every table with its row count and the columns that can be read."""
    return await asyncio.to_thread(_schema)
