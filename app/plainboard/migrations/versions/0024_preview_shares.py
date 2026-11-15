"""previews can be shared: public or with a password, for a limited time

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preview_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, unique=True),
        sa.Column("access", sa.String(20), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=True),
        sa.Column("created_by_agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_preview_shares_project_id", "preview_shares", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_preview_shares_project_id", table_name="preview_shares")
    op.drop_table("preview_shares")
