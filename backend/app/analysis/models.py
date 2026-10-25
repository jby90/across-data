from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AgentWorkflowConfig(Base):
    """Dify 风格多智能体工作流配置表(支持配置导入导出与历史版本管理,生产锁定只读)。"""

    __tablename__ = "agent_workflow_configs"
    __table_args__ = (
        UniqueConstraint("workflow_key", "version_no", name="uq_workflow_key_version"),
        Index(
            "uq_workflow_active", "workflow_key", unique=True, postgresql_where=text("is_active")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    dsl_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="cyshuhe-dify-dsl/1.0"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    readonly_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    runtime_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nodes_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    edges_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalysisTask(Base):
    """多智能体合规分析任务表(支持普通企业用户按租户隔离发起、追问补充事实、生成八维度报告)。"""

    __tablename__ = "analysis_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('collecting_facts','ready_to_run','running','completed',"
            "'partial_sample','failed','awaiting_user_facts')",
            name="ck_analysis_tasks_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    country_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    vehicle_model: Mapped[str] = mapped_column(String(128), nullable=False)
    scenario_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="collecting_facts", index=True
    )
    facts_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    missing_facts_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    sufficiency_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mcp_calls_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    backtrack_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fact_followup_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    pending_user_facts_json: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    fact_followup_history_json: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    analysis_checkpoint_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_workflow_configs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    workflow_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AnalysisStepTrace(Base):
    """多智能体协同步骤轨迹表(记录每个 Agent 节点输入输出、MCP 工具调用、核验与退回补证)。"""

    __tablename__ = "analysis_step_traces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workflow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_workflow_configs.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_node_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="passed")
    business_summary: Mapped[str] = mapped_column(Text, nullable=False)
    is_backtrack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backtrack_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    backtrack_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    input_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output_snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReportVersion(Base):
    """八维度安全评估报告不可变版本表(支持整份与按维度拆分、编辑、对比与导出)。"""

    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("task_id", "version_no", name="uq_report_task_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="full_draft")
    is_partial_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False)
    executive_summary: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    stale_impacts_json: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalysisMessage(Base):
    """分析对话消息:用户与助手的逐轮问答(信息采集全部在对话里完成)。"""

    __tablename__ = "analysis_messages"
    __table_args__ = (UniqueConstraint("task_id", "seq_no", name="uq_analysis_msg_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    slot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
