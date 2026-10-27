"""Durable analysis progress metadata, no rewrite of existing records.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "analysis_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", pg.UUID(as_uuid=True), sa.ForeignKey("organizations.id")),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_analysis_events_task_id", "analysis_events", ["task_id"])
    op.create_index("ix_analysis_events_org_id", "analysis_events", ["org_id"])


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM analysis_events) THEN
        RAISE EXCEPTION 'retain progress history; downgrade refused'; END IF; END $$;""")
    op.drop_table("analysis_events")
