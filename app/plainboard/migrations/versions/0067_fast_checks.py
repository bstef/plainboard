"""PlainBoard has slow check runs made faster, by default; a project can switch it off

Revision ID: 0067
Revises: 0066

The check runs of trains are measured; at the first look, when they grew clearly beyond the
repository's baseline (the verdict of the last speed-up) or are over the owner's target, the manager
is told to have them made faster (without checking less).
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
        batch.add_column(sa.Column("check_target_seconds", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("checks_baseline_seconds", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("checks_baseline_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("checks_verdict", sa.String(300), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("checks_verdict")
        batch.drop_column("checks_baseline_at")
        batch.drop_column("checks_baseline_seconds")
        batch.drop_column("check_target_seconds")
        batch.drop_column("slow_told_seconds")
        batch.drop_column("slow_told_at")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("fast_checks")
