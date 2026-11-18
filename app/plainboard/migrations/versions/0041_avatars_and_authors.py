"""profile pictures, and who wrote a chat message

Revision ID: 0041
Revises: 0040

A user can have a profile picture, and a chat message knows the person who wrote it, so the chat
shows name and picture instead of a name in brackets inside the text.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("avatar", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("avatar_type", sa.String(30), nullable=True))
        batch.add_column(sa.Column("avatar_version", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("author_name", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("author_name")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("avatar_version")
        batch.drop_column("avatar_type")
        batch.drop_column("avatar")
