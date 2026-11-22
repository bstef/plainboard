"""what each person's projects may use, and whose project a project is

Revision ID: 0059
Revises: 0058
"""
from alembic import op
import sqlalchemy as sa

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("max_projects", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("account_ids", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("runner_ids", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("max_parallel", sa.Integer(), nullable=True))
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_projects_owner_id", "users", ["owner_id"], ["id"], ondelete="SET NULL")
    # A project so far: its first owner (the one who created it, as a rule).
    op.execute("UPDATE projects SET owner_id = (SELECT MIN(user_id) FROM project_members WHERE project_members.project_id = projects.id AND project_members.role = 'owner')")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_owner_id", type_="foreignkey")
        batch.drop_column("owner_id")
    with op.batch_alter_table("users") as batch:
        for column in ("max_parallel", "runner_ids", "account_ids", "max_projects"):
            batch.drop_column(column)
