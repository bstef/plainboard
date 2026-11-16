"""pausing the whole team in one step

Revision ID: 0032
Revises: 0031

Which members were paused together, so that resuming the team enables exactly those again and not
a member the user had switched off before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("team_pause", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("team_pause")
