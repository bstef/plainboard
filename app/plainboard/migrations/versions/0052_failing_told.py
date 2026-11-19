"""when the manager was last told that a repository's check runs keep failing

Revision ID: 0052
Revises: 0051
"""
from alembic import op
import sqlalchemy as sa

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("failing_told_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("failing_told_at")
