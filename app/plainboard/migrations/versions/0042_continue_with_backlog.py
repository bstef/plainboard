"""a project goes on with its backlog

Revision ID: 0042
Revises: 0041

When nothing in To do can start, the team takes the next tickets from the Backlog instead of
resting. On for every project; a project that keeps its Backlog for later switches it off.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("continue_with_backlog", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("continue_with_backlog")
