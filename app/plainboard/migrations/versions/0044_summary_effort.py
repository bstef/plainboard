"""an agent's own thinking effort for its summaries

Revision ID: 0044
Revises: 0043

Empty: the installation's setting, as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.add_column(sa.Column("summary_effort", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("summary_effort")
