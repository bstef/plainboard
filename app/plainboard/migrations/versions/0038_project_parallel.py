"""parallel agents per project

Revision ID: 0038
Revises: 0037

The limit of members working at once counts per project. A project can have its own number; without
one the number from the settings applies, to each project by itself.
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("parallel_agents", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("parallel_agents")
