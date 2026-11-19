"""a landing remembers its conflict

Revision ID: 0049
Revises: 0048

Since when a branch could not be merged, and in which files while its author resolves that.
"""
from alembic import op
import sqlalchemy as sa

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        batch.add_column(sa.Column("conflict_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("conflict_files", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        batch.drop_column("conflict_files")
        batch.drop_column("conflict_at")
