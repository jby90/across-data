"""Business projections. Administrator diagnostics remain unchanged in storage/API."""

from pydantic import ConfigDict, Field, computed_field, field_serializer

from app.analysis.schemas import AnalysisTaskDetailOut, AnalysisTaskSummaryOut

PHASES = {
    "start": "准备评估",
    "fact_check": "整理业务信息",
    "retrieval": "查找适用法规",
    "analysis": "分析合规要求",
    "verification": "核对报告依据",
    "re_analysis": "补充分析",
    "re_verification": "核对补充依据",
    "compilation": "编制报告",
    "update_linkage": "检查法规更新",
    "user_fact_question": "等待补充信息",
    "user_fact_answer": "已补充信息",
    "error": "评估暂未完成",
}
FINDING_FIELDS = {
    "finding_id",
    "dimension_code",
    "business_fact",
    "clause_id",
    "document_id",
    "document_title",
    "clause_number",
    "version_id",
    "version_no",
    "source_url",
    "compliance_obligation",
    "risk_level",
    "basis_quote",
    "basis_zh",
    "source_nature",
    "source_notice",
}
CITATION_FIELDS = {
    "citation_tag",
    "clause_id",
    "document_id",
    "document_title",
    "clause_number",
    "version_id",
    "version_no",
    "source_url",
    "quote_original",
    "quote_zh",
    "review_status",
    "source_review_status",
    "translation_review_status",
    "language",
    "source_nature",
    "source_notice",
}


class PublicTaskDetail(AnalysisTaskDetailOut):
    model_config = ConfigDict(from_attributes=True)
    step_traces: list = Field(exclude=True)
    total_tokens: int = Field(exclude=True)
    total_duration_ms: int = Field(exclude=True)
    mcp_calls_count: int = Field(exclude=True)
    backtrack_count: int = Field(exclude=True)
    workflow_version_id: object = Field(default=None, exclude=True)
    workflow_version_no: int = Field(exclude=True)
    current_active_node_id: str | None = Field(default=None, exclude=True)
    current_active_node_title: str | None = Field(default=None, exclude=True)

    @field_serializer("findings")
    def business_findings(self, findings):
        return [
            {k: v for k, v in f.model_dump(mode="json").items() if k in FINDING_FIELDS}
            for f in findings
        ]

    @field_serializer("report_versions")
    def business_reports(self, reports):
        result = []
        for report in reports:
            data = report.model_dump(mode="json")
            for dimension in data["dimensions"]:
                dimension["findings"] = [
                    {k: v for k, v in finding.items() if k in FINDING_FIELDS}
                    for finding in dimension["findings"]
                ]
                dimension["citations"] = [
                    {k: v for k, v in citation.items() if k in CITATION_FIELDS}
                    for citation in dimension["citations"]
                ]
            result.append(data)
        return result

    @computed_field
    @property
    def progress(self) -> list[dict[str, str]]:
        stages = {}
        for tr in self.step_traces:
            phase = tr.phase if hasattr(tr, "phase") else tr.get("phase")
            status = tr.status if hasattr(tr, "status") else tr.get("status")
            if phase in PHASES:
                stages[phase] = {"label": PHASES[phase], "state": (
                    "failed" if status == "failed" else
                    "waiting" if status == "awaiting_user_facts" else
                    "warning" if status in {"warning", "backtracked"} else "complete"
                )}
        result = list(stages.values())
        if self.status == "awaiting_user_facts":
            result.append({"label": "请补充业务信息", "state": "waiting"})
        elif self.status == "failed":
            result.append({"label": "评估暂未完成，可重试或联系管理员", "state": "failed"})
        return result


class PublicTaskSummary(AnalysisTaskSummaryOut):
    model_config = ConfigDict(from_attributes=True)
    total_tokens: int = Field(exclude=True)
    total_duration_ms: int = Field(exclude=True)
    mcp_calls_count: int = Field(exclude=True)
    backtrack_count: int = Field(exclude=True)
    current_active_node_id: str | None = Field(default=None, exclude=True)
    current_active_node_title: str | None = Field(default=None, exclude=True)
