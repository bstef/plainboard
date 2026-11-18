"""a project can be paused for good

Revision ID: 0047
Revises: 0046

Since when, and by whom. Empty: it is not paused.
"""
from alembic import op
import sqlalchemy as sa

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("paused_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("paused_by", sa.String(length=100), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("paused_by")
        batch.drop_column("paused_at")
