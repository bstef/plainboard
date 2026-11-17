"""the assignment a branch was handed over in

Revision ID: 0036
Revises: 0035

To know whether the fix of a repair is on its way: the branch that was handed over while that
repair was worked on, not any branch of the same agent.
"""
from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        batch.add_column(sa.Column("assignment_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("landings") as batch:
        batch.drop_column("assignment_id")
