"""a project can be limited to some accounts

Revision ID: 0051
Revises: 0050

Empty for every project: any account, as before.
"""
from alembic import op
import sqlalchemy as sa

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("account_ids", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("account_ids")
