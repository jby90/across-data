"""analysis task status: add running and failed

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 20:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = "'collecting_facts','ready_to_run','running','completed','partial_sample','failed'"
_OLD = "'collecting_facts','ready_to_run','completed','partial_sample'"


def upgrade() -> None:
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status", "analysis_tasks", f"status IN ({_NEW})"
    )


def downgrade() -> None:
    op.execute("UPDATE analysis_tasks SET status='partial_sample' WHERE status='failed'")
    op.execute("UPDATE analysis_tasks SET status='ready_to_run' WHERE status='running'")
    op.drop_constraint("ck_analysis_tasks_status", "analysis_tasks", type_="check")
    op.create_check_constraint(
        "ck_analysis_tasks_status", "analysis_tasks", f"status IN ({_OLD})"
    )
