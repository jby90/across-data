"""p3 multi-agent workflow configs, analysis tasks, step traces, and report versions

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22 19:12:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_workflow_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workflow_key", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "dsl_version",
            sa.String(length=64),
            server_default="cyshuhe-dify-dsl/1.0",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("readonly_locked", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("nodes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("edges_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("workflow_key", "version_no", name="uq_workflow_key_version"),
    )
    op.create_index(
        "ix_agent_workflow_configs_workflow_key",
        "agent_workflow_configs",
        ["workflow_key"],
    )
    op.create_index(
        "ix_agent_workflow_configs_is_active",
        "agent_workflow_configs",
        ["is_active"],
    )

    op.create_table(
        "analysis_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("country_code", sa.String(length=8), nullable=False),
        sa.Column("vehicle_model", sa.String(length=128), nullable=False),
        sa.Column("scenario_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="collecting_facts",
            nullable=False,
        ),
        sa.Column("facts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("missing_facts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sufficiency_score", sa.Integer(), server_default="0", nullable=False),
        sa.Column("findings_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("mcp_calls_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("backtrack_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("workflow_version_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('collecting_facts','ready_to_run','completed','partial_sample')",
            name="ck_analysis_tasks_status",
        ),
    )
    op.create_index("ix_analysis_tasks_org_id", "analysis_tasks", ["org_id"])
    op.create_index("ix_analysis_tasks_country_code", "analysis_tasks", ["country_code"])
    op.create_index("ix_analysis_tasks_status", "analysis_tasks", ["status"])

    op.create_table(
        "analysis_step_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq_no", sa.Integer(), nullable=False),
        sa.Column("agent_node_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="passed", nullable=False),
        sa.Column("business_summary", sa.Text(), nullable=False),
        sa.Column("is_backtrack", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("backtrack_reason", sa.Text(), nullable=True),
        sa.Column("backtrack_resolution", sa.Text(), nullable=True),
        sa.Column("tool_calls_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tokens_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_analysis_step_traces_task_id", "analysis_step_traces", ["task_id"])
    op.create_index(
        "ix_analysis_step_traces_agent_node_id",
        "analysis_step_traces",
        ["agent_node_id"],
    )

    op.create_table(
        "report_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="full_draft", nullable=False),
        sa.Column(
            "is_partial_sample",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("change_note", sa.Text(), nullable=False),
        sa.Column("executive_summary", sa.Text(), nullable=False),
        sa.Column("dimensions_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stale_impacts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("task_id", "version_no", name="uq_report_task_version"),
    )
    op.create_index("ix_report_versions_task_id", "report_versions", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_report_versions_task_id", table_name="report_versions")
    op.drop_table("report_versions")
    op.drop_index("ix_analysis_step_traces_agent_node_id", table_name="analysis_step_traces")
    op.drop_index("ix_analysis_step_traces_task_id", table_name="analysis_step_traces")
    op.drop_table("analysis_step_traces")
    op.drop_index("ix_analysis_tasks_status", table_name="analysis_tasks")
    op.drop_index("ix_analysis_tasks_country_code", table_name="analysis_tasks")
    op.drop_index("ix_analysis_tasks_org_id", table_name="analysis_tasks")
    op.drop_table("analysis_tasks")
    op.drop_index(
        "ix_agent_workflow_configs_is_active",
        table_name="agent_workflow_configs",
    )
    op.drop_index(
        "ix_agent_workflow_configs_workflow_key",
        table_name="agent_workflow_configs",
    )
    op.drop_table("agent_workflow_configs")
