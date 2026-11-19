"""a schedule can leave out a run when nothing happened

Revision ID: 0050
Revises: 0049

Off for every schedule: one that looks at something outside the project has to run regardless.
"""
from alembic import op
import sqlalchemy as sa

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(sa.Column("skip_quiet", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("skip_quiet")
