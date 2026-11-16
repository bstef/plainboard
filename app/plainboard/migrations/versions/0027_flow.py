"""flow: context per piece of work, assignments that wait for their jobs, pull requests watched by PlainBoard

Revision ID: 0027
Revises: 0026

Also lowers the context limit of agents that still have the old default (every turn re-reads the
whole context, so a large one made each turn expensive) and runs the default tidy-up once a day.
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

OLD_CONTEXT_LIMIT, CONTEXT_LIMIT = 400000, 120000
OLD_TIDY_CRON = "0 */4 * * *"
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

Never delete anything the user wrote or may still need. Do not start new work, do not delegate and do
not message teammates for this. Reply in one or two lines: what you changed, or "Nothing to tidy."."""


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("title", sa.String(300), nullable=False, server_default=""),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("merge", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("checks", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("head_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("handled_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("check_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("round_started_at", sa.DateTime(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("repository_id", "number"),
    )
    op.create_index("ix_pull_requests_project_id", "pull_requests", ["project_id"])
    op.create_index("ix_pull_requests_status", "pull_requests", ["status"])
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("assignment_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_jobs_assignment_id", "assignments", ["assignment_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_jobs_assignment_id", ["assignment_id"])
    with op.batch_alter_table("assignments") as batch:
        batch.add_column(sa.Column("pull_request_id", sa.Integer(), nullable=True))
        batch.create_index("ix_assignments_pull_request_id", ["pull_request_id"])
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("session_topic", sa.String(40), nullable=True))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("job_wait_seconds", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("account_key", sa.String(40), nullable=True))

    db = op.get_bind()
    # Only agents that never got a limit of their own.
    db.execute(sa.text("UPDATE agents SET context_limit_tokens = :new WHERE context_limit_tokens = :old"), {"new": CONTEXT_LIMIT, "old": OLD_CONTEXT_LIMIT})
    # Only where the default tidy-up was not changed.
    from plainboard.services.schedules import TIDY_CRON, TIDY_NAME, TIDY_PROMPT

    db.execute(sa.text("UPDATE schedules SET cron = :new WHERE name = :name AND cron = :old"), {"new": TIDY_CRON, "name": TIDY_NAME, "old": OLD_TIDY_CRON})
    db.execute(sa.text("UPDATE schedules SET prompt = :new WHERE name = :name AND prompt = :old"), {"new": TIDY_PROMPT, "name": TIDY_NAME, "old": PREVIOUS_TIDY_PROMPT})


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("account_key")
        batch.drop_column("job_wait_seconds")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("session_topic")
    with op.batch_alter_table("assignments") as batch:
        batch.drop_index("ix_assignments_pull_request_id")
        batch.drop_column("pull_request_id")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_index("ix_jobs_assignment_id")
        batch.drop_constraint("fk_jobs_assignment_id", type_="foreignkey")
        batch.drop_column("assignment_id")
    op.drop_index("ix_pull_requests_status", table_name="pull_requests")
    op.drop_index("ix_pull_requests_project_id", table_name="pull_requests")
    op.drop_table("pull_requests")
