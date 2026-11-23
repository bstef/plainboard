"""Semantic search: every chunk names its source by foreign key, so deleting it deletes the chunk

Revision ID: 0074
Revises: 0073
"""
import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

SOURCES = {"task_id": "tasks", "assignment_id": "assignments", "decision_id": "decisions", "comment_id": "comments", "note_id": "notes", "repository_id": "repositories"}


def _there(bind) -> bool:
    return bind.dialect.name == "postgresql" and bool(bind.execute(sa.text("SELECT to_regclass('search_documents') IS NOT NULL")).scalar())


def upgrade() -> None:
    bind = op.get_bind()
    if not _there(bind):  # SQLite, or no pgvector: the tables are made with these columns where they come later
        return
    # Derived data: made anew, so that every chunk names its source (and the model chosen now embeds it).
    op.execute("DELETE FROM search_documents")
    op.execute("DELETE FROM search_cursors")
    for column, table in SOURCES.items():  # IF NOT EXISTS: made by 0073 already where it ran with today's models
        op.execute(f"ALTER TABLE search_documents ADD COLUMN IF NOT EXISTS {column} INTEGER REFERENCES {table}(id) ON DELETE CASCADE")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_search_documents_{column} ON search_documents ({column})")


def downgrade() -> None:
    if not _there(op.get_bind()):
        return
    for column in SOURCES:
        op.execute(f"DROP INDEX IF EXISTS ix_search_documents_{column}")
        op.execute(f"ALTER TABLE search_documents DROP COLUMN IF EXISTS {column}")
