"""where a ticket stands

Revision ID: 0033
Revises: 0032

One text per ticket that PlainBoard keeps from the reports of its assignments: what is done, open,
decided, and what was tried and dropped. The memory of a ticket that takes many assignments.
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_states",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("until_assignment_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ticket_states")
