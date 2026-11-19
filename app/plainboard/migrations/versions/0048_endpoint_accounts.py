"""accounts of providers that are an endpoint

Revision ID: 0048
Revises: 0047

A local model server or a model host: the account holds its address, the models it offers and how
many tokens they take at once.
"""
from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("endpoint_url", sa.String(length=500), nullable=False, server_default=""))
        batch.add_column(sa.Column("models", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("context_tokens", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("context_tokens")
        batch.drop_column("models")
        batch.drop_column("endpoint_url")
