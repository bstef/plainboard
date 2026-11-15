"""the default tidy-up also checks preview servers (only where its prompt was not changed)

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

OLD_PROMPT = """\
Routine tidy-up, so that nothing piles up that nobody needs any more. Work carefully: change only what
is clearly outdated or redundant. When in doubt, leave it as it is. Nothing here is urgent.

Look through:
- Project notes: merge duplicates, correct outdated ones, delete obsolete ones, unpin what not everyone
  must always know. Your own notes: forget what no longer applies.
- Schedules: delete those that have done their job or repeat another one (not this one).
- Tickets: move finished ones to done and archive old done ones. A ticket that looks no longer relevant
  stays; mention it in your reply instead of deleting it.
- Task lists of the team: delete tasks that are done elsewhere or no longer make sense.
- Team: do not remove or change agents; if one seems no longer needed, mention it in your reply.

Never delete anything the user wrote or may still need. Do not start new work, do not delegate and do
not message teammates for this. Reply in one or two lines: what you changed, or "Nothing to tidy."."""


def upgrade() -> None:
    from plainboard.services.schedules import TIDY_NAME, TIDY_PROMPT

    op.get_bind().execute(
        sa.text("UPDATE schedules SET prompt = :new WHERE name = :name AND prompt = :old"), {"new": TIDY_PROMPT, "name": TIDY_NAME, "old": OLD_PROMPT}
    )


def downgrade() -> None:
    pass  # an ordinary schedule: the user may have changed it since
