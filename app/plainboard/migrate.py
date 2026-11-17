"""Alembic helpers (programmatic config, no alembic.ini needed).

    python -m plainboard.migrate upgrade            # apply migrations
    python -m plainboard.migrate revision "message" # autogenerate a new migration
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from .config import get_config


def alembic_config() -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_config().sync_database_url.replace("%", "%%"))
    return cfg


def upgrade() -> None:
    config = get_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if config.postgres and _empty(config.sync_database_url):
        # The migrations up to here were written for SQLite, where every installation began. A new
        # PostgreSQL database starts at today's tables instead, and takes every migration from now on.
        from .models import Base

        engine = create_engine(config.sync_database_url)
        try:
            Base.metadata.create_all(engine)
            _first_rows(engine)
        finally:
            engine.dispose()
        command.stamp(alembic_config(), "head")
        return
    command.upgrade(alembic_config(), "head")


def _first_rows(engine) -> None:
    """What the migrations put into a new installation besides tables: the two subscriptions it starts with
    (0018), which use what the environment or the runner's own sign-in provides."""
    from .models import Account

    claude = "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "oauth_token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "runner_login"
    with Session(engine) as session:
        session.add(Account(provider="claude_code", label="Claude", key="claude", auth_mode=claude, enabled=True))
        session.add(Account(provider="codex", label="Codex", key="default", auth_mode="chatgpt", enabled=True))
        session.commit()


def _empty(url: str) -> bool:
    engine = create_engine(url)
    try:
        return not inspect(engine).get_table_names()
    finally:
        engine.dispose()


def revision(message: str) -> None:
    command.revision(alembic_config(), message=message, autogenerate=True)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "revision":
        upgrade()
        revision(sys.argv[2] if len(sys.argv) > 2 else "change")
    else:
        upgrade()
