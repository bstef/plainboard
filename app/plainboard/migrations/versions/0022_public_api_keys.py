"""several labelled public API keys per project

Revision ID: 0022
Revises: 0021
"""
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(200), nullable=False, server_default=""),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("hint", sa.String(8), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_public_api_keys_project_id", "public_api_keys", ["project_id"])
    # The one key a project could have keeps working.
    db = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    for project_id, key_hash in db.execute(sa.text("SELECT id, public_api_key_hash FROM projects WHERE public_api_key_hash IS NOT NULL")).fetchall():
        db.execute(
            sa.text("INSERT INTO public_api_keys (project_id, label, key_hash, hint, created_at) VALUES (:p, '', :h, '', :now)"),
            {"p": project_id, "h": key_hash, "now": now},
        )
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_public_api_key_hash", type_="unique")
        batch.drop_column("public_api_key_hash")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("public_api_key_hash", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_projects_public_api_key_hash", ["public_api_key_hash"])
    op.drop_index("ix_public_api_keys_project_id", "public_api_keys")
    op.drop_table("public_api_keys")
