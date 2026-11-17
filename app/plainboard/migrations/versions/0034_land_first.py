"""land first, check after

Revision ID: 0034
Revises: 0033

A repository can have its finished branches merged at once, with the checks run on the base branch
afterwards (repositories.land_first). A train of such a repository landed before its checks
(trains.direct); which checks failed is kept (trains.failed_checks), and whether the manager was
told about a base that stays red (trains.escalated).
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("land_first", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("trains") as batch:
        batch.add_column(sa.Column("direct", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("failed_checks", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("escalated", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("trains") as batch:
        batch.drop_column("escalated")
        batch.drop_column("failed_checks")
        batch.drop_column("direct")
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("land_first")
