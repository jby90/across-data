from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


def format_conversation_code(task_id: uuid.UUID, created_at: datetime | None = None) -> str:
    """为每次用户对话生成唯一且易读的对话编号,例如 CONV-20261022-8F3A1B2C。"""
    dt = created_at or datetime.now(UTC)
    return f"CONV-{dt.strftime('%Y%m%d')}-{task_id.hex[:8].upper()}"


def compute_active_node(task_status: str, traces: list[Any]) -> tuple[str | None, str | None]:
    """根据当前对话工作流状态和已完成步骤轨迹,计算当前正在运行或等待中的 Agent 节点。"""
    if task_status == "collecting_facts":
        return "node_fact_collector", "信息采集与追问 Agent（对话采集中）"
    if task_status == "awaiting_user_facts":
        return "node_fact_collector", "信息采集与追问 Agent（分析暂停，等待业务事实）"
    if task_status == "ready_to_run":
        return "node_fact_collector", "信息采集与追问 Agent（事实已齐备，待启动）"
    if task_status != "running":
        return None, None
    if not traces:
        return "node_fact_collector", "信息采集与追问 Agent（正在校验八维度事实完备度）"
    last = traces[-1]
    phase = getattr(last, "phase", None) or (last.get("phase") if isinstance(last, dict) else "")
    st = getattr(last, "status", None) or (last.get("status") if isinstance(last, dict) else "")
    is_bt = getattr(last, "is_backtrack", False) or (
        last.get("is_backtrack", False) if isinstance(last, dict) else False
    )
    if phase == "fact_check":
        return "node_mcp_retriever", "MCP 法规混合检索 Agent（正在检索目标国现行条款）"
    if phase == "user_fact_answer":
        return "node_mcp_retriever", "MCP 法规混合检索 Agent（正在恢复受影响维度）"
    if phase == "retrieval":
        return (
            "node_dimension_analyst",
            "八维度合规矩阵分析 Agent（正在并发调用模型分析 8 个维度）",
        )
    if phase == "analysis":
        return (
            "node_citation_verifier",
            "依据子串核验与退回质检 Agent（正在程序化核验原文子串与数字）",
        )
    if phase == "verification":
        if st == "backtracked" or is_bt:
            return (
                "node_dimension_analyst",
                "八维度合规矩阵分析 Agent（按质检退回意见补证重分析中）",
            )
        return "node_report_compiler", "八维度国别报告编制 Agent（正在汇总生成八维度报告）"
    if phase == "re_analysis":
        return "node_citation_verifier", "依据子串核验与退回质检 Agent（正在二次核验补证结论）"
    if phase == "re_verification":
        return "node_report_compiler", "八维度国别报告编制 Agent（正在汇总生成八维度报告）"
    if phase == "compilation":
        return (
            "node_update_linkage",
            "法规修订 72h 影响联动 Agent（正在扫描法规版本修订影响）",
        )
    return "node_report_compiler", "八维度国别报告编制 Agent（收尾处理中）"


class WorkflowMcpToolSchema(BaseModel):
    tool_name: str
    display_name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class WorkflowNodeOut(BaseModel):
    node_id: str
    title: str
    subtitle: str
    node_type: str  # trigger / collector / retriever / analyst / verifier / compiler / linkage
    category_label: str
    icon: str
    color: str
    position_x: int
    position_y: int
    readonly_locked: bool = True
    lock_reason: str = "生产运行锁定:仅允许查看流程与连接详情,禁止在线调试模型参数或修改节点代码"
    model_provider: str
    model_name: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    system_prompt: str
    code_entrypoint: str
    mcp_tools: list[WorkflowMcpToolSchema] = Field(default_factory=list)
    input_variables: list[dict[str, str]] = Field(default_factory=list)
    output_variables: list[dict[str, str]] = Field(default_factory=list)
    responsibility_desc: str
    runtime_settings: dict[str, Any] = Field(default_factory=dict)
    configurable_fields: list[str] = Field(default_factory=list)


class WorkflowEdgeOut(BaseModel):
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str  # forward / backtrack / event_trigger
    label: str
    condition_desc: str
    animated: bool = False


class AgentWorkflowConfigOut(BaseModel):
    id: uuid.UUID
    workflow_key: str
    version_no: int
    name: str
    description: str
    dsl_version: str
    is_active: bool
    readonly_locked: bool
    runtime_schema_version: str | None = None
    executable: bool = False
    nodes: list[WorkflowNodeOut]
    edges: list[WorkflowEdgeOut]
    change_note: str | None = None
    created_at: datetime


class WorkflowVersionSummaryOut(BaseModel):
    id: uuid.UUID
    version_no: int
    name: str
    dsl_version: str
    is_active: bool
    readonly_locked: bool
    executable: bool = False
    nodes_count: int
    edges_count: int
    change_note: str | None = None
    created_at: datetime


class WorkflowDslImportIn(BaseModel):
    dsl_payload: dict[str, Any]
    change_note: str = Field(default="管理员导入多智能体编排配置", max_length=500)
    activate_immediately: bool = False


class FollowUpQuestionOut(BaseModel):
    fact_key: str
    dimension_code: str
    question_zh: str
    why_needed: str
    suggested_options: list[str] = Field(default_factory=list)
    current_value: str | None = None
    is_fulfilled: bool = False


class AnalysisFindingOut(BaseModel):
    version_id: str | None = None
    source_nature: str = "unclassified"
    source_notice: str = ""
    selected_sentence_ids: list[int] = Field(default_factory=list)
    anchor_quote_start: int | None = None
    anchor_quote_end: int | None = None
    sentence_ids: list[int] = Field(default_factory=list)
    quote_start: int | None = None
    quote_end: int | None = None
    source_text_sha256: str | None = None
    selection_version: str | None = None
    finding_id: str
    dimension_code: str
    business_fact: str
    clause_id: str | None = None
    document_id: str | None = None
    document_title: str
    clause_number: str
    version_no: int = 1
    source_url: str | None = None
    compliance_obligation: str
    risk_level: str  # HIGH / MEDIUM / LOW / PENDING_VERIFICATION
    basis_quote: str
    basis_zh: str
    verification_status: str  # VERIFIED_SUBSTRING / SOURCE_EVIDENCE_PENDING


class ToolCallTraceOut(BaseModel):
    call_id: str
    tool_name: str
    tool_label: str
    arguments: dict[str, Any]
    result_summary: str
    duration_ms: int
    verified: bool = True


class AnalysisStepTraceOut(BaseModel):
    workflow_version_id: uuid.UUID | None = None
    id: uuid.UUID
    seq_no: int
    agent_node_id: str
    agent_name: str
    phase: str
    status: str
    business_summary: str
    is_backtrack: bool
    backtrack_reason: str | None = None
    backtrack_resolution: str | None = None
    tool_calls: list[ToolCallTraceOut] = Field(default_factory=list)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    tokens_used: int
    duration_ms: int
    created_at: datetime


class ReportCitationOut(BaseModel):
    source_review_status: str = "unrecorded"
    translation_review_status: str = "unrecorded"
    translation_id: str | None = None
    language: str = ""
    version_id: str | None = None
    source_nature: str = "unclassified"
    source_notice: str = ""
    selected_sentence_ids: list[int] = Field(default_factory=list)
    anchor_quote_start: int | None = None
    anchor_quote_end: int | None = None
    sentence_ids: list[int] = Field(default_factory=list)
    quote_start: int | None = None
    quote_end: int | None = None
    source_text_sha256: str | None = None
    selection_version: str | None = None
    citation_tag: str  # e.g. "[SG-PDPA-s26]"
    clause_id: str | None = None
    document_id: str | None = None
    document_title: str
    clause_number: str
    version_no: int = 1
    source_url: str | None = None
    quote_original: str
    quote_zh: str
    review_status: str  # approved / pending_review / machine_translated


class ReportDimensionOut(BaseModel):
    findings: list[dict[str, Any]] = Field(default_factory=list)
    gap_kind: str | None = None
    dimension_code: str  # D1..D8
    dimension_title: str
    status: str  # COMPLETE / PARTIAL_GAP / STALE_ALERT
    risk_level: str
    content_markdown: str
    key_recommendations: list[str] = Field(default_factory=list)
    citations: list[ReportCitationOut] = Field(default_factory=list)
    missing_gap_note: str | None = None


class StaleImpactAlertOut(BaseModel):
    dimension_code: str
    clause_ref: str
    amendment_summary: str
    action_required: str
    detected_at: str


class ReportVersionOut(BaseModel):
    regulatory_updates: list[dict[str, Any]] = Field(default_factory=list)
    citation_policy: str = "legacy_unrecorded"
    citation_review_summary: dict[str, int] = Field(default_factory=dict)
    source_integrity_alerts: list[str] = Field(default_factory=list)
    id: uuid.UUID
    task_id: uuid.UUID
    version_no: int
    status: str
    is_partial_sample: bool
    change_note: str
    executive_summary: str
    dimensions: list[ReportDimensionOut]
    stale_impacts: list[StaleImpactAlertOut] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def summarize_reviews(self):
        from app.analysis.citation_policy import review_summary

        self.citation_review_summary = review_summary([d.model_dump() for d in self.dimensions])
        return self


class AnalysisTaskSummaryOut(BaseModel):
    id: uuid.UUID
    conversation_code: str = ""
    org_id: uuid.UUID | None = None
    org_name: str = ""
    creator_email: str = ""
    creator_name: str = ""
    title: str
    country_code: str
    country_name_zh: str
    vehicle_model: str
    scenario_type: str
    scenario_label: str
    status: str
    sufficiency_score: int
    missing_facts_count: int
    messages_count: int = 0
    steps_count: int = 0
    current_active_node_id: str | None = None
    current_active_node_title: str | None = None
    findings_count: int
    latest_report_version: int
    is_partial_sample: bool
    total_tokens: int
    total_duration_ms: int
    mcp_calls_count: int
    backtrack_count: int
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    seq_no: int
    role: str
    content: str
    options: list[str] = Field(default_factory=list)
    slot: str | None = None
    created_at: datetime


class ChatMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    question_id: uuid.UUID | None = None


class AnalysisTaskDetailOut(BaseModel):
    id: uuid.UUID
    conversation_code: str = ""
    org_id: uuid.UUID | None = None
    org_name: str = ""
    creator_email: str = ""
    creator_name: str = ""
    current_active_node_id: str | None = None
    current_active_node_title: str | None = None
    title: str
    country_code: str
    country_name_zh: str
    vehicle_model: str
    scenario_type: str
    scenario_label: str
    status: str
    sufficiency_score: int
    facts: dict[str, Any]
    missing_facts: list[FollowUpQuestionOut]
    findings: list[AnalysisFindingOut]
    step_traces: list[AnalysisStepTraceOut]
    report_versions: list[ReportVersionOut]
    total_tokens: int
    total_duration_ms: int
    mcp_calls_count: int
    backtrack_count: int
    workflow_version_id: uuid.UUID | None = None
    workflow_version_no: int
    messages: list[ChatMessageOut] = Field(default_factory=list)
    pending_user_facts: list[dict[str, Any]] = Field(default_factory=list)
    fact_followup_rounds: int = 0
    created_at: datetime
    updated_at: datetime


class AnalysisTaskCreateIn(BaseModel):
    title: str = Field(min_length=2, max_length=256)
    country_code: str = Field(default="SG", min_length=2, max_length=2)
    vehicle_model: str = Field(default="CYS-Pilot L4 Autonomous SUV", max_length=128)
    scenario_type: str = Field(default="autonomous_trial", max_length=64)
    facts: dict[str, Any] = Field(default_factory=dict)
    auto_run_if_sufficient: bool = True


class UpdateFactsIn(BaseModel):
    facts: dict[str, Any]
    auto_run_if_sufficient: bool = True
    question_ids: dict[str, str] = Field(default_factory=dict)


class ReportEditIn(BaseModel):
    change_note: str = Field(default="业务人员在线修订报告章节", max_length=500)
    executive_summary: str | None = None
    dimension_updates: dict[str, str] = Field(
        default_factory=dict,
        description="dimension_code (D1..D8) -> 修订后的 content_markdown",
    )


class DimensionDiffItemOut(BaseModel):
    dimension_code: str
    dimension_title: str
    changed: bool
    from_markdown: str
    to_markdown: str


class ReportDiffOut(BaseModel):
    task_id: uuid.UUID
    from_version: int
    to_version: int
    summary_changed: bool
    from_summary: str
    to_summary: str
    dimension_diffs: list[DimensionDiffItemOut]


class BenchmarkCaseOut(BaseModel):
    question_id: str
    country_code: str
    dimension_code: str
    scenario_question: str
    required_phrases: list[str]
    matched_document_title: str
    matched_clause_label: str
    passed: bool
    evidence_snippet: str


class BenchmarkSummaryOut(BaseModel):
    total_cases: int
    hit_cases: int
    retrieval_hit_rate_pct: float
    human_review_status: str
    note: str
    cases: list[BenchmarkCaseOut]
