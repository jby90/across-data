"""Pin executable workflow versions without inventing historical execution provenance.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_workflow_configs", sa.Column("runtime_schema_version", sa.String(32)))
    for table in ("analysis_tasks", "analysis_step_traces"):
        op.add_column(table, sa.Column("workflow_version_id", postgresql.UUID(as_uuid=True)))
        op.create_foreign_key(
            f"fk_{table}_workflow",
            table,
            "agent_workflow_configs",
            ["workflow_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_workflow_version_id", table, ["workflow_version_id"])
    op.create_index(
        "uq_workflow_active",
        "agent_workflow_configs",
        ["workflow_key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.execute("""
    CREATE FUNCTION protect_workflow_content() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workflow versions are append-only';
      END IF;
      IF (to_jsonb(OLD) - 'is_active') IS DISTINCT FROM (to_jsonb(NEW) - 'is_active') THEN
        RAISE EXCEPTION 'workflow version content is immutable';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER immutable_workflow_content BEFORE UPDATE OR DELETE ON agent_workflow_configs
      FOR EACH ROW EXECUTE FUNCTION protect_workflow_content();
    """)


def downgrade():
    op.execute("DROP TRIGGER immutable_workflow_content ON agent_workflow_configs")
    op.execute("DROP FUNCTION protect_workflow_content()")
    op.drop_index("uq_workflow_active", table_name="agent_workflow_configs")
    for table in ("analysis_step_traces", "analysis_tasks"):
        op.drop_index(f"ix_{table}_workflow_version_id", table_name=table)
        op.drop_constraint(f"fk_{table}_workflow", table, type_="foreignkey")
        op.drop_column(table, "workflow_version_id")
    op.drop_column("agent_workflow_configs", "runtime_schema_version")
