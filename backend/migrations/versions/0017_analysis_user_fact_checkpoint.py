"""Add durable user-fact pauses without rewriting source, reports or approvals.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "analysis_tasks",
        sa.Column("fact_followup_rounds", sa.Integer(), nullable=False, server_default="0"),
    )
    for name, default in [
        ("pending_user_facts_json", "[]"),
        ("fact_followup_history_json", "[]"),
        ("analysis_checkpoint_json", "{}"),
    ]:
        op.add_column(
            "analysis_tasks",
            sa.Column(
                name,
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'" + default + "'::jsonb"),
            ),
        )
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('collecting_facts','ready_to_run','running','completed',"
        "'partial_sample','failed','awaiting_user_facts')",
    )


def downgrade():
    # Never discard an outstanding question/history or silently reset a paused task.
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT FROM analysis_tasks WHERE fact_followup_rounds > 0
                OR analysis_checkpoint_json <> '{}'::jsonb) THEN
        RAISE EXCEPTION 'retain user-fact history/checkpoints; downgrade refused';
      END IF;
    END $$;""")
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status",
        "analysis_tasks",
        "status IN ('collecting_facts','ready_to_run','running','completed',"
        "'partial_sample','failed')",
    )
    for name in [
        "analysis_checkpoint_json",
        "fact_followup_history_json",
        "pending_user_facts_json",
        "fact_followup_rounds",
    ]:
        op.drop_column("analysis_tasks", name)
