"""a project's subscription, and whether the installation is set up

Revision ID: 0040
Revises: 0039

A project can name the subscription its agents run on. An installation that already has projects
has been set up by hand: it is not asked to go through the basic settings again.
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_projects_account_id", "accounts", ["account_id"], ["id"], ondelete="SET NULL")
    bind = op.get_bind()
    if bind.execute(sa.text("select count(*) from projects")).scalar():
        bind.execute(sa.text("delete from settings where key = 'setup_done'"))
        bind.execute(sa.text("insert into settings (key, value, secret) values ('setup_done', 'true', 0)"))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("fk_projects_account_id", type_="foreignkey")
        batch.drop_column("account_id")
