"""project.review_days: tickets left in review move to done; the tidy-up looks at the review column

Revision ID: 0026
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
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
- Tickets: move finished ones to done and archive old done ones. A ticket that looks no longer relevant
  stays; mention it in your reply instead of deleting it.
- Task lists of the team: delete tasks that are done elsewhere or no longer make sense.
- Background jobs of the team (list_jobs): check for each preview or dev server whether it is still
  needed. Stop it with stop_job when its work is finished or it has been running for more than a day
  without anyone using it, and say so in your reply. Leave builds, installs and tests alone.
- Team: do not remove or change agents; if one seems no longer needed, mention it in your reply.

Never delete anything the user wrote or may still need. Do not start new work, do not delegate and do
not message teammates for this. Reply in one or two lines: what you changed, or "Nothing to tidy."."""


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("review_days", sa.Integer(), nullable=False, server_default="3"))
    # Only where the default tidy-up's prompt was not changed.
    from plainboard.services.schedules import TIDY_NAME, TIDY_PROMPT

    op.get_bind().execute(
        sa.text("UPDATE schedules SET prompt = :new WHERE name = :name AND prompt = :old"),
        {"new": TIDY_PROMPT, "name": TIDY_NAME, "old": PREVIOUS_TIDY_PROMPT},
    )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("review_days")
