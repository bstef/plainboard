"""quick check at hand-over, and one repair for a train that failed

Revision ID: 0029
Revises: 0028

A repository can have a quick check: PlainBoard runs it when a branch is handed over, so what it
catches never costs a check run. An assignment can be the repair of a whole train.
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("quick_check_command", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("assignments") as batch:
        batch.add_column(sa.Column("train_id", sa.Integer(), nullable=True))
    op.create_index("ix_assignments_train_id", "assignments", ["train_id"])


def downgrade() -> None:
    op.drop_index("ix_assignments_train_id", table_name="assignments")
    with op.batch_alter_table("assignments") as batch:
        batch.drop_column("train_id")
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("quick_check_command")
