"""thinking effort for managers, members and summaries

Revision ID: 0043
Revises: 0042

One default effort became three. An installation that chose its own keeps that choice for managers
and members; summaries had none and start with the new default.
"""
from alembic import op
import sqlalchemy as sa

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text), sa.column("secret", sa.Boolean))


def upgrade() -> None:
    bind = op.get_bind()
    chosen = bind.execute(sa.select(settings.c.value).where(settings.c.key == "default_effort")).first()
    if chosen is None:
        return
    for key in ("default_manager_effort", "default_worker_effort"):
        if bind.execute(sa.select(settings.c.key).where(settings.c.key == key)).first() is None:
            bind.execute(settings.insert().values(key=key, value=chosen[0], secret=False))
    bind.execute(settings.delete().where(settings.c.key == "default_effort"))


def downgrade() -> None:
    bind = op.get_bind()
    chosen = bind.execute(sa.select(settings.c.value).where(settings.c.key == "default_worker_effort")).first()
    if chosen is not None:
        bind.execute(settings.insert().values(key="default_effort", value=chosen[0], secret=False))
    bind.execute(settings.delete().where(settings.c.key.in_(("default_manager_effort", "default_worker_effort", "default_summary_effort"))))
