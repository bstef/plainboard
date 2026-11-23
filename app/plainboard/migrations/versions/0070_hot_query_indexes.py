"""Indexes for the queries that scanned the big tables: a turn's start and end (runs of an agent by
status, its last finished run, its unread updates), a ticket's or assignment's runs, the statistics
and checks over a project's recent runs and an agent's recent messages

Revision ID: 0070
Revises: 0069
"""
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_runs_agent_id_status", "runs", ["agent_id", "status"]),
    ("ix_runs_agent_id_finished_at", "runs", ["agent_id", "finished_at"]),
    ("ix_runs_project_id_created_at", "runs", ["project_id", "created_at"]),
    ("ix_runs_task_id", "runs", ["task_id"]),
    ("ix_runs_assignment_id", "runs", ["assignment_id"]),
    ("ix_inbox_items_agent_id_consumed_at", "inbox_items", ["agent_id", "consumed_at"]),
    ("ix_messages_conversation_id_created_at", "messages", ["conversation_id", "created_at"]),
]


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    for name, table, _columns in INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)
