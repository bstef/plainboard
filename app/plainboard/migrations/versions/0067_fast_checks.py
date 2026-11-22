"""PlainBoard has slow check runs made faster, by default; a project can switch it off

Revision ID: 0067
Revises: 0066

The check runs of trains are measured; when a repository's are slow or grew clearly slower, the
manager is told once to have them made faster (without checking less), at most every few days.
"""
from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("fast_checks", sa.Boolean(), nullable=False, server_default=sa.true()))
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("slow_told_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("slow_told_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("slow_told_seconds")
        batch.drop_column("slow_told_at")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("fast_checks")
