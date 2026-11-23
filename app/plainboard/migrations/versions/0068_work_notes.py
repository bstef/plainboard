"""Working notes per assignment: they survive context compression and go when the assignment ends

Revision ID: 0068
Revises: 0067
"""
from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.add_column(sa.Column("work_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("work_notes")
