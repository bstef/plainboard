"""what the managers heard about an external runner's self-update

Revision ID: 0058
Revises: 0057
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runners") as batch:
        batch.add_column(sa.Column("update_told", sa.String(80), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("runners") as batch:
        batch.drop_column("update_told")
