"""a repository can have its merged branches removed

Revision ID: 0046
Revises: 0045

Off for every repository: deleting in someone's repository is asked for, not assumed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("tidy_branches", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("tidied_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("tidied_at")
        batch.drop_column("tidy_branches")
