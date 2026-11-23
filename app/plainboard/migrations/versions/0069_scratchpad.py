"""The working notes are the scratchpad: archived for a while after the assignment, not dropped at its end

Revision ID: 0069
Revises: 0068
"""
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.alter_column("work_notes", new_column_name="scratchpad")


def downgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.alter_column("scratchpad", new_column_name="work_notes")
