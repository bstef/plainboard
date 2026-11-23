"""Full tool details are kept for a while (tool_details_days, 30 for new installations); an existing one keeps them forever
until its admin decides, so an update deletes nothing by itself

Revision ID: 0071
Revises: 0070
"""
import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(sa.text("SELECT 1 FROM users LIMIT 1")).first() or bind.execute(sa.text("SELECT 1 FROM messages LIMIT 1")).first()
    there = bind.execute(sa.text("SELECT 1 FROM settings WHERE key = 'tool_details_days'")).first()
    if used and not there:
        bind.execute(sa.text("INSERT INTO settings (key, value, secret) VALUES ('tool_details_days', '0', :no)"), {"no": False})


def downgrade() -> None:
    op.execute("DELETE FROM settings WHERE key IN ('tool_details_days', 'tool_details_floor')")
