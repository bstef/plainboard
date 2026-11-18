"""the language a project's agents write to people in

Revision ID: 0045
Revises: 0044

Empty: the language each person writes in.
"""
from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("language", sa.String(length=50), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("language")
