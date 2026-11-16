"""trains: finished branches land together, with one check run for all of them

Revision ID: 0028
Revises: 0027

The pull requests of 0027 become landings (a branch that was handed over, with or without a pull
request of its own); trains are the batches they are checked in. A repository can have a check
command, which makes this work with any git host.
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("pull_requests", "landings")
    with op.batch_alter_table("landings") as batch:
        batch.add_column(sa.Column("branch", sa.String(250), nullable=False, server_default=""))
        batch.add_column(sa.Column("train_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("group_no", sa.Integer(), nullable=True))
        batch.alter_column("number", existing_type=sa.Integer(), nullable=True)
        for column in ("merge", "checks", "rounds", "check_seconds", "round_started_at"):
            batch.drop_column(column)
    op.create_index("ix_landings_train_id", "landings", ["train_id"])
    op.create_table(
        "trains",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.Integer(), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="checking"),
        sa.Column("branch", sa.String(250), nullable=False, server_default=""),
        sa.Column("base", sa.String(250), nullable=False, server_default=""),
        sa.Column("head_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(500), nullable=False, server_default=""),
        sa.Column("job_id", sa.String(32), nullable=True),
        sa.Column("group_no", sa.Integer(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("check_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_trains_project_id", "trains", ["project_id"])
    op.create_index("ix_trains_repository_id", "trains", ["repository_id"])
    op.create_index("ix_trains_status", "trains", ["status"])
    # One step each: a batch cannot rename a column and index it under its new name at once.
    op.drop_index("ix_assignments_pull_request_id", table_name="assignments")
    with op.batch_alter_table("assignments") as batch:
        batch.alter_column("pull_request_id", new_column_name="landing_id")
    op.create_index("ix_assignments_landing_id", "assignments", ["landing_id"])
    with op.batch_alter_table("repositories") as batch:
        batch.add_column(sa.Column("check_command", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("repositories") as batch:
        batch.drop_column("check_command")
    op.drop_index("ix_assignments_landing_id", table_name="assignments")
    with op.batch_alter_table("assignments") as batch:
        batch.alter_column("landing_id", new_column_name="pull_request_id")
    op.create_index("ix_assignments_pull_request_id", "assignments", ["pull_request_id"])
    op.drop_table("trains")
    op.drop_index("ix_landings_train_id", table_name="landings")
    with op.batch_alter_table("landings") as batch:
        batch.drop_column("group_no")
        batch.drop_column("train_id")
        batch.drop_column("branch")
        batch.add_column(sa.Column("merge", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("checks", sa.String(20), nullable=False, server_default="pending"))
        batch.add_column(sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("check_seconds", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("round_started_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    op.rename_table("landings", "pull_requests")
