"""a person's own subscriptions, and with whom they are shared

Revision ID: 0061
Revises: 0060
"""
from alembic import op
import sqlalchemy as sa

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("share", sa.String(20), nullable=False, server_default="private"))
        batch.add_column(sa.Column("shared_with", sa.JSON(), nullable=True))
        batch.create_foreign_key("fk_accounts_owner_id", "users", ["owner_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("fk_accounts_owner_id", type_="foreignkey")
        for column in ("shared_with", "share", "owner_id"):
            batch.drop_column(column)
