"""existing projects get the manager's default tidy-up schedule

Revision ID: 0021
Revises: 0020
"""
from datetime import UTC, datetime, timedelta

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from plainboard.services.schedules import TIDY_CRON, TIDY_NAME, TIDY_PROMPT

    db = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    # Next full 4-hour mark (UTC); the scheduler computes the following ones from the cron expression.
    first = now.replace(minute=0, second=0) + timedelta(hours=4 - now.hour % 4)
    managers = db.execute(sa.text("SELECT id, project_id FROM agents WHERE is_manager = 1")).fetchall()
    for agent_id, project_id in managers:
        exists = db.execute(sa.text("SELECT 1 FROM schedules WHERE project_id = :p AND name = :n"), {"p": project_id, "n": TIDY_NAME}).first()
        if exists:
            continue
        db.execute(
            sa.text(
                "INSERT INTO schedules (project_id, agent_id, name, prompt, kind, cron, timezone, enabled, next_run_at, created_at, updated_at) "
                "VALUES (:p, :a, :n, :prompt, 'cron', :cron, 'UTC', 1, :next, :now, :now)"
            ),
            {"p": project_id, "a": agent_id, "n": TIDY_NAME, "prompt": TIDY_PROMPT, "cron": TIDY_CRON, "next": first, "now": now},
        )


def downgrade() -> None:
    pass  # the schedules stay: they are ordinary schedules the user may have changed
