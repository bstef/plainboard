"""Semantic search: a switch per project, and on PostgreSQL with pgvector the extension and the index tables

Revision ID: 0073
Revises: 0072
"""
import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("semantic_search", sa.Boolean(), nullable=False, server_default=sa.true()))
    from plainboard.services.search.schema import ensure

    ensure(op.get_bind())  # nothing on SQLite or without pgvector; then the feature is absent


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_vectors")
    op.execute("DROP TABLE IF EXISTS search_documents")
    op.execute("DROP TABLE IF EXISTS search_cursors")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("semantic_search")
