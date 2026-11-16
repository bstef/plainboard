"""a log of what PlainBoard did by itself

Revision ID: 0030
Revises: 0029

The other tables keep the latest state. To improve the flow, its course is needed too: which
branches a train left with, why it failed, who repaired it, which wait was ended.
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flow_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("assignment_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("landing_id", sa.Integer(), nullable=True),
        sa.Column("train_id", sa.Integer(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_flow_events_project_id", "flow_events", ["project_id"])
    op.create_index("ix_flow_events_at", "flow_events", ["at"])


def downgrade() -> None:
    op.drop_index("ix_flow_events_at", table_name="flow_events")
    op.drop_index("ix_flow_events_project_id", table_name="flow_events")
    op.drop_table("flow_events")
