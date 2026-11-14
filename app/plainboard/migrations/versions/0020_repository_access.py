"""repositories: own access (token or SSH key)

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("auth", sa.String(20), nullable=False, server_default="default"))
        batch.add_column(sa.Column("token_user", sa.String(200), nullable=False, server_default=""))
        batch.add_column(sa.Column("token_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ssh_key_enc", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ssh_public_key", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        for column in ("ssh_public_key", "ssh_key_enc", "token_enc", "token_user", "auth"):
            batch.drop_column(column)
