"""Personal access tokens (MCP clients, the API) and the audit of MCP tool calls

Revision ID: 0075
Revises: 0074
"""
import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_personal_tokens_user_id", "personal_tokens", ["user_id"])
    op.create_table(
        "mcp_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(60), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.String(300), nullable=False, server_default=""),
    )
    op.create_index("ix_mcp_calls_at", "mcp_calls", ["at"])
    op.create_index("ix_mcp_calls_user_id", "mcp_calls", ["user_id"])


def downgrade() -> None:
    op.drop_table("mcp_calls")
    op.drop_table("personal_tokens")
