"""when a waiting chat message was read

Revision ID: 0035
Revises: 0034

A chat message for a busy agent waits as its next turn. The agent may read it before that turn
starts (after the step it is in). The chat shows the message at the bottom until then, and from
then on at the place where it was read.
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("read_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("read_at")
