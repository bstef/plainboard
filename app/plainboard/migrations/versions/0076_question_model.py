"""The model and effort of a project's questions (Ctrl+Enter in the chat, orchestration/questions.py)

Revision ID: 0076
Revises: 0075
"""
import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

COLUMNS = (("ask_provider", 50), ("ask_model", 100), ("ask_effort", 20))


def upgrade() -> None:
    for name, size in COLUMNS:
        op.add_column("projects", sa.Column(name, sa.String(size), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        for name, _ in COLUMNS:
            batch.drop_column(name)
