"""the daily tidy-up looks at PlainBoard's own checks of the flow

Revision ID: 0037
Revises: 0036

Only where the default tidy-up was not changed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

PREVIOUS_TIDY_PROMPT = """\
Routine tidy-up, so that nothing piles up that nobody needs any more. Work carefully: change only what
is clearly outdated or redundant. When in doubt, leave it as it is. Nothing here is urgent.

Look through:
- Project notes: merge duplicates, correct outdated ones, delete obsolete ones, unpin what not everyone
  must always know. Your own notes: forget what no longer applies. Own notes of team members
  (member_notes): only where they look long or old, delete what clearly no longer applies, never notes
  from the user.
- Schedules: delete those that have done their job or repeat another one (not this one).
- Tickets: move finished ones to done, also those in review that the user does not have to look at, and
  archive old done ones. A ticket that looks no longer relevant stays; mention it in your reply instead
  of deleting it.
- Task lists of the team: delete tasks that are done elsewhere or no longer make sense.
- Background jobs of the team (list_jobs): check for each preview or dev server whether it is still
  needed. Stop it with stop_job when its work is finished or it has been running for more than a day
  without anyone using it, and say so in your reply. Leave builds, installs and tests alone.
- Team: do not remove or change agents; if one seems no longer needed, mention it in your reply.
- Waiting time (time_report): if one command or the checks of pull requests make up most of the
  team's waiting and no ticket for it exists, create one to make that faster or needed less often.

Never delete anything the user wrote or may still need. Do not start new work, do not delegate and do
not message teammates for this. Reply in one or two lines: what you changed, or "Nothing to tidy."."""


def upgrade() -> None:
    from plainboard.services.schedules import TIDY_NAME, TIDY_PROMPT

    op.get_bind().execute(sa.text("UPDATE schedules SET prompt = :new WHERE name = :name AND prompt = :old"), {"new": TIDY_PROMPT, "name": TIDY_NAME, "old": PREVIOUS_TIDY_PROMPT})


def downgrade() -> None:
    from plainboard.services.schedules import TIDY_NAME, TIDY_PROMPT

    op.get_bind().execute(sa.text("UPDATE schedules SET prompt = :old WHERE name = :name AND prompt = :new"), {"new": TIDY_PROMPT, "name": TIDY_NAME, "old": PREVIOUS_TIDY_PROMPT})
