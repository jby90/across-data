from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import (
    AgentWorkflowConfig,
    AnalysisStepTrace,
    AnalysisTask,
    ReportVersion,
)
from app.analysis.report_exporter import build_report_docx, build_report_pdf
from app.analysis.schemas import (
    AgentWorkflowConfigOut,
    AnalysisFindingOut,
    AnalysisStepTraceOut,
    AnalysisTaskCreateIn,
    AnalysisTaskDetailOut,
    AnalysisTaskSummaryOut,
    BenchmarkSummaryOut,
    DimensionDiffItemOut,
    FollowUpQuestionOut,
    ReportDiffOut,
    ReportDimensionOut,
    ReportEditIn,
    ReportVersionOut,
    StaleImpactAlertOut,
    ToolCallTraceOut,
    UpdateFactsIn,
    WorkflowDslImportIn,
    WorkflowEdgeOut,
    WorkflowNodeOut,
    WorkflowVersionSummaryOut,
)
from app.analysis.workflow_engine import (
    COUNTRY_NAMES_ZH,
    SCENARIO_LABELS,
    WORKFLOW_KEY,
    ensure_p3_seed_data,
    ensure_workflow_config,
    evaluate_fact_sufficiency,
    execute_task_pipeline,
    export_workflow_dsl,
    run_evaluation_benchmarks,
    validate_and_import_workflow_dsl,
)
from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_user_side
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import ClauseTranslation, KgEdge, KgNode
from app.knowledge.router import _compute_eight_attributes
from app.knowledge.schemas import GraphPayloadOut, KgEdgeOut, KgNodeOut

AdminOrReviewerDep = Annotated[Principal, Depends(require_admin_side)]
UserDep = Annotated[Principal, Depends(require_user_side)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

admin_router = APIRouter(prefix="/api/admin/agent-studio", tags=["admin-agent-studio"])
user_router = APIRouter(prefix="/api/analysis", tags=["user-compliance-analysis"])


def _to_workflow_out(cfg: AgentWorkflowConfig) -> AgentWorkflowConfigOut:
    return AgentWorkflowConfigOut(
        id=cfg.id,
        workflow_key=cfg.workflow_key,
        version_no=cfg.version_no,
        name=cfg.name,
        description=cfg.description,
        dsl_version=cfg.dsl_version,
        is_active=cfg.is_active,
        readonly_locked=True,
        nodes=[WorkflowNodeOut(**n) for n in (cfg.nodes_json or [])],
        edges=[WorkflowEdgeOut(**e) for e in (cfg.edges_json or [])],
        change_note=cfg.change_note,
        created_at=cfg.created_at,
    )


async def _to_task_summary(session: AsyncSession, t: AnalysisTask) -> AnalysisTaskSummaryOut:
    rvs = list(
        (
            await session.execute(
                select(ReportVersion)
                .where(ReportVersion.task_id == t.id)
                .order_by(ReportVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_ver = rvs[0].version_no if rvs else 0
    is_partial = rvs[0].is_partial_sample if rvs else (t.status == "partial_sample")
    missing_unfulfilled = sum(
        1 for m in (t.missing_facts_json or []) if not m.get("is_fulfilled", False)
    )
    return AnalysisTaskSummaryOut(
        id=t.id,
        title=t.title,
        country_code=t.country_code,
        country_name_zh=COUNTRY_NAMES_ZH.get(t.country_code, t.country_code),
        vehicle_model=t.vehicle_model,
        scenario_type=t.scenario_type,
        scenario_label=SCENARIO_LABELS.get(t.scenario_type, t.scenario_type),
        status=t.status,
        sufficiency_score=t.sufficiency_score,
        missing_facts_count=missing_unfulfilled,
        findings_count=len(t.findings_json or []),
        latest_report_version=latest_ver,
        is_partial_sample=is_partial,
        total_tokens=t.total_tokens,
        total_duration_ms=t.total_duration_ms,
        mcp_calls_count=t.mcp_calls_count,
        backtrack_count=t.backtrack_count,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


async def _to_task_detail(session: AsyncSession, t: AnalysisTask) -> AnalysisTaskDetailOut:
    traces = list(
        (
            await session.execute(
                select(AnalysisStepTrace)
                .where(AnalysisStepTrace.task_id == t.id)
                .order_by(AnalysisStepTrace.seq_no.asc())
            )
        )
        .scalars()
        .all()
    )
    rvs = list(
        (
            await session.execute(
                select(ReportVersion)
                .where(ReportVersion.task_id == t.id)
                .order_by(ReportVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )

    trace_outs = [
        AnalysisStepTraceOut(
            id=tr.id,
            seq_no=tr.seq_no,
            agent_node_id=tr.agent_node_id,
            agent_name=tr.agent_name,
            phase=tr.phase,
            status=tr.status,
            business_summary=tr.business_summary,
            is_backtrack=tr.is_backtrack,
            backtrack_reason=tr.backtrack_reason,
            backtrack_resolution=tr.backtrack_resolution,
            tool_calls=[ToolCallTraceOut(**tc) for tc in (tr.tool_calls_json or [])],
            input_snapshot=tr.input_snapshot_json or {},
            output_snapshot=tr.output_snapshot_json or {},
            tokens_used=tr.tokens_used,
            duration_ms=tr.duration_ms,
            created_at=tr.created_at,
        )
        for tr in traces
    ]

    rv_outs = [
        ReportVersionOut(
            id=rv.id,
            task_id=rv.task_id,
            version_no=rv.version_no,
            status=rv.status,
            is_partial_sample=rv.is_partial_sample,
            change_note=rv.change_note,
            executive_summary=rv.executive_summary,
            dimensions=[ReportDimensionOut(**d) for d in (rv.dimensions_json or [])],
            stale_impacts=[StaleImpactAlertOut(**s) for s in (rv.stale_impacts_json or [])],
            created_at=rv.created_at,
        )
        for rv in rvs
    ]

    _, evaluated_missing = evaluate_fact_sufficiency(t.country_code, t.facts_json or {})

    return AnalysisTaskDetailOut(
        id=t.id,
        title=t.title,
        country_code=t.country_code,
        country_name_zh=COUNTRY_NAMES_ZH.get(t.country_code, t.country_code),
        vehicle_model=t.vehicle_model,
        scenario_type=t.scenario_type,
        scenario_label=SCENARIO_LABELS.get(t.scenario_type, t.scenario_type),
        status=t.status,
        sufficiency_score=t.sufficiency_score,
        facts=t.facts_json or {},
        missing_facts=[FollowUpQuestionOut(**m) for m in evaluated_missing],
        findings=[AnalysisFindingOut(**f) for f in (t.findings_json or [])],
        step_traces=trace_outs,
        report_versions=rv_outs,
        total_tokens=t.total_tokens,
        total_duration_ms=t.total_duration_ms,
        mcp_calls_count=t.mcp_calls_count,
        backtrack_count=t.backtrack_count,
        workflow_version_no=t.workflow_version_no,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


# ==================== 管理端 Dify 风格只读多智能体工作台接口 ====================


@admin_router.get("/workflow", response_model=AgentWorkflowConfigOut)
async def get_admin_workflow(
    _: AdminOrReviewerDep,
    session: SessionDep,
    version_no: int | None = Query(default=None),
) -> AgentWorkflowConfigOut:
    """查看当前激活(或指定版本)的多智能体编排拓扑、节点详情与上下游连接(只读锁定态)。"""
    await ensure_p3_seed_data(session)
    if version_no is not None:
        cfg = (
            await session.execute(
                select(AgentWorkflowConfig).where(
                    AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
                    AgentWorkflowConfig.version_no == version_no,
                )
            )
        ).scalar_one_or_none()
        if cfg is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="指定工作流版本不存在"
            )
        return _to_workflow_out(cfg)

    cfg = await ensure_workflow_config(session)
    return _to_workflow_out(cfg)


@admin_router.get("/workflow/versions", response_model=list[WorkflowVersionSummaryOut])
async def list_workflow_versions(
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> list[WorkflowVersionSummaryOut]:
    await ensure_p3_seed_data(session)
    cfgs = list(
        (
            await session.execute(
                select(AgentWorkflowConfig)
                .where(AgentWorkflowConfig.workflow_key == WORKFLOW_KEY)
                .order_by(AgentWorkflowConfig.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        WorkflowVersionSummaryOut(
            id=c.id,
            version_no=c.version_no,
            name=c.name,
            dsl_version=c.dsl_version,
            is_active=c.is_active,
            readonly_locked=True,
            nodes_count=len(c.nodes_json or []),
            edges_count=len(c.edges_json or []),
            change_note=c.change_note,
            created_at=c.created_at,
        )
        for c in cfgs
    ]


@admin_router.post(
    "/workflow/versions/{version_no}/activate",
    response_model=AgentWorkflowConfigOut,
)
async def activate_workflow_version(
    version_no: int,
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> AgentWorkflowConfigOut:
    cfgs = list(
        (
            await session.execute(
                select(AgentWorkflowConfig).where(AgentWorkflowConfig.workflow_key == WORKFLOW_KEY)
            )
        )
        .scalars()
        .all()
    )
    target = next((c for c in cfgs if c.version_no == version_no), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标工作流版本不存在")
    for c in cfgs:
        c.is_active = c.version_no == version_no
    await session.commit()
    await session.refresh(target)
    return _to_workflow_out(target)


@admin_router.get("/workflow/export")
async def export_admin_workflow_dsl(
    _: AdminOrReviewerDep,
    session: SessionDep,
    version_no: int | None = Query(default=None),
) -> dict:
    """导出多智能体编排配置包(JSON DSL),供长久维护、跨环境迁移与版本备份。"""
    await ensure_p3_seed_data(session)
    if version_no is not None:
        cfg = (
            await session.execute(
                select(AgentWorkflowConfig).where(
                    AgentWorkflowConfig.workflow_key == WORKFLOW_KEY,
                    AgentWorkflowConfig.version_no == version_no,
                )
            )
        ).scalar_one_or_none()
        if cfg is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工作流版本不存在")
    else:
        cfg = await ensure_workflow_config(session)
    return export_workflow_dsl(cfg)


@admin_router.post(
    "/workflow/import",
    response_model=AgentWorkflowConfigOut,
    status_code=status.HTTP_201_CREATED,
)
async def import_admin_workflow_dsl(
    body: WorkflowDslImportIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> AgentWorkflowConfigOut:
    """导入并校验多智能体编排配置包(JSON DSL),生成新版本并保持生产只读安全锁。"""
    await ensure_p3_seed_data(session)
    try:
        new_cfg = await validate_and_import_workflow_dsl(
            session=session,
            dsl_payload=body.dsl_payload,
            change_note=body.change_note,
            user_id=user.user_id,
            activate_immediately=body.activate_immediately,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _to_workflow_out(new_cfg)


@admin_router.get("/tasks", response_model=list[AnalysisTaskSummaryOut])
async def list_admin_analysis_tasks(
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> list[AnalysisTaskSummaryOut]:
    await ensure_p3_seed_data(session)
    tasks = list(
        (await session.execute(select(AnalysisTask).order_by(AnalysisTask.created_at.asc())))
        .scalars()
        .all()
    )
    return [await _to_task_summary(session, t) for t in tasks]


@admin_router.get("/tasks/{task_id}", response_model=AnalysisTaskDetailOut)
async def get_admin_analysis_task_detail(
    task_id: uuid.UUID,
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    return await _to_task_detail(session, task)


@admin_router.get("/benchmarks", response_model=BenchmarkSummaryOut)
async def get_admin_benchmarks(
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> BenchmarkSummaryOut:
    await ensure_p3_seed_data(session)
    res = await run_evaluation_benchmarks(session)
    return BenchmarkSummaryOut(**res)


# ==================== 业务用户端多智能体合规分析与八维度报告接口 ====================


@user_router.get("/tasks", response_model=list[AnalysisTaskSummaryOut])
async def list_user_analysis_tasks(
    user: UserDep,
    session: SessionDep,
    country_code: str | None = None,
) -> list[AnalysisTaskSummaryOut]:
    await ensure_p3_seed_data(session)
    stmt = select(AnalysisTask).where(
        or_(AnalysisTask.org_id == user.org_id, AnalysisTask.org_id.is_(None))
    )
    if country_code:
        stmt = stmt.where(AnalysisTask.country_code == country_code.upper())
    stmt = stmt.order_by(AnalysisTask.created_at.asc())
    tasks = list((await session.execute(stmt)).scalars().all())
    return [await _to_task_summary(session, t) for t in tasks]


@user_router.post(
    "/tasks",
    response_model=AnalysisTaskDetailOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_analysis_task(
    body: AnalysisTaskCreateIn,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    await ensure_p3_seed_data(session)
    cc = body.country_code.upper()
    score, missing = evaluate_fact_sufficiency(cc, body.facts or {})
    task = AnalysisTask(
        org_id=user.org_id,
        created_by=user.user_id,
        title=body.title.strip(),
        country_code=cc,
        vehicle_model=body.vehicle_model.strip(),
        scenario_type=body.scenario_type.strip(),
        status="ready_to_run" if score >= 100 else "collecting_facts",
        facts_json=body.facts or {},
        missing_facts_json=missing,
        sufficiency_score=score,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    if score >= 100 and body.auto_run_if_sufficient:
        task = await execute_task_pipeline(session, task)

    return await _to_task_detail(session, task)


@user_router.get("/tasks/{task_id}", response_model=AnalysisTaskDetailOut)
async def get_user_analysis_task_detail(
    task_id: uuid.UUID,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/facts", response_model=AnalysisTaskDetailOut)
async def update_user_task_facts(
    task_id: uuid.UUID,
    body: UpdateFactsIn,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    """补充追问事实；当事实完备度达到 100% 时自动启动多智能体协同生成八维度报告。"""
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")

    merged = dict(task.facts_json or {})
    for k, v in (body.facts or {}).items():
        if v is not None and str(v).strip():
            merged[k] = str(v).strip()

    score, missing = evaluate_fact_sufficiency(task.country_code, merged)
    task.facts_json = merged
    task.missing_facts_json = missing
    task.sufficiency_score = score
    await session.commit()
    await session.refresh(task)

    if score >= 100 and body.auto_run_if_sufficient:
        task = await execute_task_pipeline(session, task)

    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/run", response_model=AnalysisTaskDetailOut)
async def run_user_analysis_task(
    task_id: uuid.UUID,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    if task.sufficiency_score < 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必填业务事实尚未补充完整，请先完成追问补充后再启动多智能体分析",
        )
    task = await execute_task_pipeline(session, task)
    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/reports/edit", response_model=ReportVersionOut)
async def edit_user_report_version(
    task_id: uuid.UUID,
    body: ReportEditIn,
    user: UserDep,
    session: SessionDep,
) -> ReportVersionOut:
    """在线编辑报告章节并保存为新的不可变版本(保留部分样稿标识)。"""
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")

    rvs = list(
        (
            await session.execute(
                select(ReportVersion)
                .where(ReportVersion.task_id == task.id)
                .order_by(ReportVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rvs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前任务尚无报告版本")

    latest = rvs[0]
    next_ver = latest.version_no + 1
    new_dims: list[dict] = []
    for d in latest.dimensions_json or []:
        copy_d = dict(d)
        code = copy_d.get("dimension_code", "")
        if code in (body.dimension_updates or {}):
            copy_d["content_markdown"] = body.dimension_updates[code]
        new_dims.append(copy_d)

    new_rv = ReportVersion(
        task_id=task.id,
        version_no=next_ver,
        status="partial_sample" if latest.is_partial_sample else "edited",
        is_partial_sample=latest.is_partial_sample,
        change_note=body.change_note.strip() or f"人工修订版本 v{next_ver}",
        executive_summary=(
            body.executive_summary.strip()
            if body.executive_summary and body.executive_summary.strip()
            else latest.executive_summary
        ),
        dimensions_json=new_dims,
        stale_impacts_json=latest.stale_impacts_json or [],
        created_by=user.user_id,
    )
    session.add(new_rv)
    await session.commit()
    await session.refresh(new_rv)

    return ReportVersionOut(
        id=new_rv.id,
        task_id=new_rv.task_id,
        version_no=new_rv.version_no,
        status=new_rv.status,
        is_partial_sample=new_rv.is_partial_sample,
        change_note=new_rv.change_note,
        executive_summary=new_rv.executive_summary,
        dimensions=[ReportDimensionOut(**d) for d in (new_rv.dimensions_json or [])],
        stale_impacts=[StaleImpactAlertOut(**s) for s in (new_rv.stale_impacts_json or [])],
        created_at=new_rv.created_at,
    )


@user_router.get("/tasks/{task_id}/reports/diff", response_model=ReportDiffOut)
async def diff_user_report_versions(
    task_id: uuid.UUID,
    user: UserDep,
    session: SessionDep,
    from_version: int = Query(default=1, ge=1),
    to_version: int = Query(default=2, ge=1),
) -> ReportDiffOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")

    rv_from = (
        await session.execute(
            select(ReportVersion).where(
                ReportVersion.task_id == task.id,
                ReportVersion.version_no == from_version,
            )
        )
    ).scalar_one_or_none()
    rv_to = (
        await session.execute(
            select(ReportVersion).where(
                ReportVersion.task_id == task.id,
                ReportVersion.version_no == to_version,
            )
        )
    ).scalar_one_or_none()
    if rv_from is None or rv_to is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对比的报告版本不存在")

    from_map = {d["dimension_code"]: d for d in (rv_from.dimensions_json or [])}
    to_map = {d["dimension_code"]: d for d in (rv_to.dimensions_json or [])}

    diffs: list[DimensionDiffItemOut] = []
    for code, to_d in to_map.items():
        from_d = from_map.get(code, {})
        f_md = str(from_d.get("content_markdown", ""))
        t_md = str(to_d.get("content_markdown", ""))
        diffs.append(
            DimensionDiffItemOut(
                dimension_code=code,
                dimension_title=str(to_d.get("dimension_title", code)),
                changed=(f_md != t_md),
                from_markdown=f_md,
                to_markdown=t_md,
            )
        )

    return ReportDiffOut(
        task_id=task.id,
        from_version=from_version,
        to_version=to_version,
        summary_changed=(rv_from.executive_summary != rv_to.executive_summary),
        from_summary=rv_from.executive_summary,
        to_summary=rv_to.executive_summary,
        dimension_diffs=diffs,
    )


@user_router.get("/tasks/{task_id}/reports/{version_no}/export.docx")
async def export_user_report_docx(
    task_id: uuid.UUID,
    version_no: int,
    user: UserDep,
    session: SessionDep,
) -> Response:
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    rv = (
        await session.execute(
            select(ReportVersion).where(
                ReportVersion.task_id == task.id,
                ReportVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if rv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告版本不存在")

    docx_bytes = build_report_docx(task, rv)
    filename = f"cyshuhe_{task.country_code}_8dim_report_v{version_no}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@user_router.get("/tasks/{task_id}/reports/{version_no}/export.pdf")
async def export_user_report_pdf(
    task_id: uuid.UUID,
    version_no: int,
    user: UserDep,
    session: SessionDep,
) -> Response:
    task = await session.get(AnalysisTask, task_id)
    if task is None or (task.org_id is not None and task.org_id != user.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    rv = (
        await session.execute(
            select(ReportVersion).where(
                ReportVersion.task_id == task.id,
                ReportVersion.version_no == version_no,
            )
        )
    ).scalar_one_or_none()
    if rv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告版本不存在")

    pdf_bytes = build_report_pdf(task, rv)
    filename = f"cyshuhe_{task.country_code}_8dim_report_v{version_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@user_router.get("/clauses/{clause_id}/citation-card")
async def get_clause_citation_card(
    clause_id: uuid.UUID,
    _: UserDep,
    session: SessionDep,
) -> dict:
    """点击报告引用角标或分析矩阵条款时，返回原文、译文、版本及 1 跳关联图谱。"""
    row = (
        await session.execute(
            select(Clause, DocumentVersion, Document)
            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Clause.id == clause_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用条款不存在")

    clause, ver, doc = row
    tr = (
        await session.execute(
            select(ClauseTranslation).where(ClauseTranslation.clause_id == clause.id)
        )
    ).scalar_one_or_none()

    edges = list(
        (await session.execute(select(KgEdge).where(KgEdge.evidence_clause_id == clause.id)))
        .scalars()
        .all()
    )
    if not edges:
        # 若当前条款未直接挂边，取同法规的语义边展示 1 跳关联面板
        sibling_cids = list(
            (await session.execute(select(Clause.id).where(Clause.version_id == ver.id)))
            .scalars()
            .all()
        )
        if sibling_cids:
            edges = list(
                (
                    await session.execute(
                        select(KgEdge).where(KgEdge.evidence_clause_id.in_(sibling_cids))
                    )
                )
                .scalars()
                .all()
            )

    node_ids: set[uuid.UUID] = set()
    for e in edges:
        node_ids.add(e.source_node_id)
        node_ids.add(e.target_node_id)

    nodes = (
        list((await session.execute(select(KgNode).where(KgNode.id.in_(node_ids)))).scalars().all())
        if node_ids
        else []
    )
    node_map = {n.id: n for n in nodes}

    node_outs = [
        KgNodeOut(
            id=n.id,
            country_code=n.country_code,
            node_type=n.node_type,
            node_key=n.node_key,
            name_zh=n.name_zh,
            name_orig=n.name_orig,
            summary=n.summary,
            document_id=n.document_id,
            evidence_clause_id=n.evidence_clause_id,
            review_status=n.review_status,
            attributes=_compute_eight_attributes(
                n.country_code, n.node_type, n.name_zh, n.attributes
            ),
        )
        for n in nodes
    ]
    edge_outs = [
        KgEdgeOut(
            id=e.id,
            country_code=e.country_code,
            source_node_id=e.source_node_id,
            source_name_zh=node_map[e.source_node_id].name_zh
            if e.source_node_id in node_map
            else "",
            source_node_type=node_map[e.source_node_id].node_type
            if e.source_node_id in node_map
            else "",
            target_node_id=e.target_node_id,
            target_name_zh=node_map[e.target_node_id].name_zh
            if e.target_node_id in node_map
            else "",
            target_node_type=node_map[e.target_node_id].node_type
            if e.target_node_id in node_map
            else "",
            relation_type=e.relation_type,
            evidence_clause_id=e.evidence_clause_id,
            evidence_quote=e.evidence_quote,
            clause_number=clause.clause_number,
            document_title=doc.title,
            extractor_model=e.extractor_model,
            review_status=e.review_status,
            usable_as_legal_basis=(e.review_status == "approved"),
        )
        for e in edges
        if e.source_node_id in node_map and e.target_node_id in node_map
    ]

    mini_graph = GraphPayloadOut(
        country_code=doc.country_code,
        node_counts_by_type={},
        edge_counts_by_relation={},
        quality_summary={},
        nodes=node_outs,
        edges=edge_outs,
    )

    return {
        "clause_id": str(clause.id),
        "document_id": str(doc.id),
        "document_title": doc.title,
        "country_code": doc.country_code,
        "clause_number": clause.clause_number or f"#{clause.ordinal}",
        "heading": clause.heading,
        "version_no": ver.version_no,
        "source_url": doc.source_url,
        "original_text": clause.original_text,
        "translated_text": tr.translated_text if tr else "（待翻译）",
        "review_status": tr.review_status if tr else "pending_review",
        "mini_graph": mini_graph.model_dump(mode="json"),
    }


@user_router.get("/benchmarks", response_model=BenchmarkSummaryOut)
async def get_user_benchmarks(
    _: UserDep,
    session: SessionDep,
) -> BenchmarkSummaryOut:
    await ensure_p3_seed_data(session)
    res = await run_evaluation_benchmarks(session)
    return BenchmarkSummaryOut(**res)
