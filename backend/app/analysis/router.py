from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.benchmarks import run_retrieval_benchmarks
from app.analysis.chat import PLACEHOLDER_COUNTRY, post_user_message, start_chat
from app.analysis.events import append_event, stream_events, validate_reader
from app.analysis.models import (
    AgentWorkflowConfig,
    AnalysisEvent,
    AnalysisMessage,
    AnalysisStepTrace,
    AnalysisTask,
    ReportVersion,
)
from app.analysis.pipeline import launch_pipeline
from app.analysis.public_view import PublicTaskDetail, PublicTaskSummary
from app.analysis.report_exporter import (
    build_report_docx,
    build_report_pdf,
    build_report_zip,
    export_filename,
)
from app.analysis.schemas import (
    AgentWorkflowConfigOut,
    AnalysisFindingOut,
    AnalysisStepTraceOut,
    AnalysisTaskCreateIn,
    AnalysisTaskDetailOut,
    AnalysisTaskSummaryOut,
    BenchmarkSummaryOut,
    ChatMessageIn,
    ChatMessageOut,
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
    compute_active_node,
    format_conversation_code,
)
from app.analysis.workflow_engine import (
    COUNTRY_NAMES_ZH,
    SCENARIO_LABELS,
    WORKFLOW_KEY,
    ensure_workflow_config,
    evaluate_fact_sufficiency,
    export_workflow_dsl,
    validate_and_import_workflow_dsl,
)
from app.analysis.workflow_runtime import (
    PARAMETERS,
    RUNTIME_SCHEMA,
    activate_version,
    execution_settings,
    pin_active_version,
    require_executable,
)
from app.core.db import get_session
from app.iam.deps import (
    Principal,
    require_admin_side,
    require_platform_admin,
    require_user_side,
)
from app.iam.models import Organization, User
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import report_held_citation_ids
from app.knowledge.models import ClauseTranslation
from app.updates.service import report_updates

AdminOrReviewerDep = Annotated[Principal, Depends(require_admin_side)]
PlatformAdminDep = Annotated[Principal, Depends(require_platform_admin)]
UserDep = Annotated[Principal, Depends(require_user_side)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

admin_router = APIRouter(prefix="/api/admin/agent-studio", tags=["admin-agent-studio"])
user_router = APIRouter(prefix="/api/analysis", tags=["user-compliance-analysis"])


STALE_RUNNING_SECONDS = 600


async def _start(session: AsyncSession, task: AnalysisTask) -> None:
    """把任务置为运行中并在后台启动流水线,请求立即返回。"""
    task = await session.scalar(
        select(AnalysisTask)
        .where(AnalysisTask.id == task.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task.status in {"running", "awaiting_user_facts"}:
        raise HTTPException(409, "任务正在执行或等待业务事实，不能重复启动或跳过追问")
    try:
        if (task.analysis_checkpoint_json or {}).get("resume_dimensions"):
            cfg = await session.get(AgentWorkflowConfig, task.workflow_version_id)
            if cfg is None:
                raise ValueError("断点的执行版本不存在")
            require_executable(cfg)
        else:
            await pin_active_version(session, task)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    task.status = "running"
    await append_event(session, task, "phase_started", {"node_id": "node_start", "phase": "start"})
    await session.commit()
    await session.refresh(task)  # updated_at 由数据库更新,需刷新后才能同步读取
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    launch_pipeline(factory, task.id)


async def _expire_stale(session: AsyncSession, task: AnalysisTask) -> None:
    """服务重启等原因导致长时间无进展的运行中任务,标记为失败,避免界面一直转圈。"""
    if task.status != "running" or task.updated_at is None:
        return
    age = (datetime.now(UTC) - task.updated_at).total_seconds()
    if age > STALE_RUNNING_SECONDS:
        task.status = "failed"
        await session.commit()
        await session.refresh(task)


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
        runtime_schema_version=cfg.runtime_schema_version,
        executable=cfg.runtime_schema_version == RUNTIME_SCHEMA,
        nodes=[
            WorkflowNodeOut(
                **n,
                runtime_settings=execution_settings(n)
                if cfg.runtime_schema_version == RUNTIME_SCHEMA
                else {},
                configurable_fields=sorted(
                    PARAMETERS.get(n["node_id"], set())
                    | (
                        {"model_name", "system_prompt"}
                        if n["node_id"] in ("node_dimension_analyst", "node_mcp_retriever")
                        else set()
                    )
                )
                if cfg.runtime_schema_version == RUNTIME_SCHEMA
                else [],
            )
            for n in (cfg.nodes_json or [])
        ],
        edges=[WorkflowEdgeOut(**e) for e in (cfg.edges_json or [])],
        change_note=cfg.change_note,
        created_at=cfg.created_at,
    )


async def _load_org_and_creator(session: AsyncSession, t: AnalysisTask) -> tuple[str, str, str]:
    org_name = "系统示例"
    if t.org_id is not None:
        org = await session.get(Organization, t.org_id)
        if org is not None:
            org_name = org.name
    creator_email = ""
    creator_name = ""
    if t.created_by is not None:
        u = await session.get(User, t.created_by)
        if u is not None:
            creator_email = u.email
            creator_name = u.display_name
    return org_name, creator_email, creator_name


def _enrich_trace_snapshots(
    t: AnalysisTask,
    tr: AnalysisStepTrace,
    rvs: list[ReportVersion],
    evaluated_missing: list[dict],
) -> tuple[dict, dict]:
    """确保历史任务与新任务的每个阶段都有可展开查看的完整输入/输出产出快照。"""
    inp = dict(tr.input_snapshot_json or {})
    out = dict(tr.output_snapshot_json or {})
    if tr.phase == "fact_check":
        inp.setdefault("country_code", t.country_code)
        inp.setdefault("vehicle_model", t.vehicle_model)
        inp.setdefault("scenario_type", t.scenario_type)
        out.setdefault("collected_facts", dict(t.facts_json or {}))
        if "fact_check_items" not in out:
            out["fact_check_items"] = [
                {
                    "fact_key": m["fact_key"],
                    "dimension_code": m["dimension_code"],
                    "question_zh": m["question_zh"],
                    "value": m.get("current_value") or "",
                    "is_fulfilled": bool(m.get("is_fulfilled")),
                }
                for m in evaluated_missing
            ]
    elif tr.phase == "compilation" and rvs:
        rv = rvs[0]
        inp.setdefault("country_code", t.country_code)
        inp.setdefault("vehicle_model", t.vehicle_model)
        inp.setdefault("verified_findings_count", len(t.findings_json or []))
        out.setdefault("report_status", rv.status)
        out.setdefault("is_partial_sample", rv.is_partial_sample)
        out.setdefault("executive_summary", rv.executive_summary)
        out.setdefault("verified_findings_count", len(t.findings_json or []))
        if "dimensions_deliverables" not in out:
            out["dimensions_deliverables"] = [
                {
                    "dimension_code": d.get("dimension_code", ""),
                    "dimension_title": d.get("dimension_title", ""),
                    "status": d.get("status", ""),
                    "risk_level": d.get("risk_level", ""),
                    "citations_count": len(d.get("citations") or []),
                    "content_markdown": d.get("content_markdown", ""),
                    "key_recommendations": list(d.get("key_recommendations") or []),
                    "missing_gap_note": d.get("missing_gap_note"),
                }
                for d in (rv.dimensions_json or [])
            ]
    elif tr.phase == "update_linkage" and rvs:
        rv = rvs[0]
        out.setdefault("stale_impacts_count", len(rv.stale_impacts_json or []))
        out.setdefault("stale_impacts", list(rv.stale_impacts_json or []))
    return inp, out


async def _to_task_summary(session: AsyncSession, t: AnalysisTask) -> AnalysisTaskSummaryOut:
    await _expire_stale(session, t)
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
    msg_count = int(
        (
            await session.execute(
                select(func.count(AnalysisMessage.id)).where(AnalysisMessage.task_id == t.id)
            )
        ).scalar_one()
        or 0
    )
    org_name, creator_email, creator_name = await _load_org_and_creator(session, t)
    active_node_id, active_node_title = compute_active_node(t.status, traces)
    latest_ver = rvs[0].version_no if rvs else 0
    is_partial = rvs[0].is_partial_sample if rvs else (t.status == "partial_sample")
    missing_unfulfilled = sum(
        1 for m in (t.missing_facts_json or []) if not m.get("is_fulfilled", False)
    )
    if t.status == "awaiting_user_facts":
        missing_unfulfilled += len(t.pending_user_facts_json or [])
    return AnalysisTaskSummaryOut(
        id=t.id,
        conversation_code=format_conversation_code(t.id, t.created_at),
        org_id=t.org_id,
        org_name=org_name,
        creator_email=creator_email,
        creator_name=creator_name,
        title=t.title,
        country_code=t.country_code,
        country_name_zh=COUNTRY_NAMES_ZH.get(t.country_code, t.country_code),
        vehicle_model=t.vehicle_model,
        scenario_type=t.scenario_type,
        scenario_label=SCENARIO_LABELS.get(t.scenario_type, t.scenario_type),
        status=t.status,
        sufficiency_score=t.sufficiency_score,
        missing_facts_count=missing_unfulfilled,
        messages_count=msg_count,
        steps_count=len(traces),
        current_active_node_id=active_node_id,
        current_active_node_title=active_node_title,
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
    await _expire_stale(session, t)
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

    _, evaluated_missing = evaluate_fact_sufficiency(t.country_code, t.facts_json or {})

    trace_outs = []
    for tr in traces:
        inp_snap, out_snap = _enrich_trace_snapshots(t, tr, rvs, evaluated_missing)
        trace_outs.append(
            AnalysisStepTraceOut(
                workflow_version_id=tr.workflow_version_id,
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
                input_snapshot=inp_snap,
                output_snapshot=out_snap,
                tokens_used=tr.tokens_used,
                duration_ms=tr.duration_ms,
                created_at=tr.created_at,
            )
        )

    rv_outs = [
        ReportVersionOut(
            regulatory_updates=await report_updates(session, rv.id),
            citation_policy=rv.citation_policy or "legacy_unrecorded",
            source_integrity_alerts=[
                f"原文绑定或页级解析待核对，引用 {cid} 已隔离，需核查后重新分析"
                for cid in await report_held_citation_ids(session, rv)
            ],
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

    msgs = list(
        (
            await session.execute(
                select(AnalysisMessage)
                .where(AnalysisMessage.task_id == t.id)
                .order_by(AnalysisMessage.seq_no.asc())
            )
        )
        .scalars()
        .all()
    )
    org_name, creator_email, creator_name = await _load_org_and_creator(session, t)
    active_node_id, active_node_title = compute_active_node(t.status, traces)
    return AnalysisTaskDetailOut(
        messages=[
            ChatMessageOut(
                id=m.id,
                seq_no=m.seq_no,
                role=m.role,
                content=m.content,
                options=list(m.options_json or []),
                slot=m.slot,
                created_at=m.created_at,
            )
            for m in msgs
        ],
        pending_user_facts=t.pending_user_facts_json or [],
        fact_followup_rounds=t.fact_followup_rounds,
        id=t.id,
        conversation_code=format_conversation_code(t.id, t.created_at),
        org_id=t.org_id,
        org_name=org_name,
        creator_email=creator_email,
        creator_name=creator_name,
        current_active_node_id=active_node_id,
        current_active_node_title=active_node_title,
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
        workflow_version_id=t.workflow_version_id,
        workflow_version_no=t.workflow_version_no,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


# ==================== 管理端 Dify 风格只读多智能体工作台接口 ====================


@admin_router.get("/citation-policy")
async def get_citation_policy(_: AdminOrReviewerDep):
    from app.core.config import get_settings

    policy = get_settings().citation_policy
    return {
        "policy": policy,
        "label": "允许机器内容进入草稿，逐条待核对"
        if policy == "allow_machine_with_notice"
        else "仅使用原文及译文均已复核的引用",
        "readonly": True,
    }


@admin_router.get("/workflow", response_model=AgentWorkflowConfigOut)
async def get_admin_workflow(
    _: AdminOrReviewerDep,
    session: SessionDep,
    version_no: int | None = Query(default=None),
) -> AgentWorkflowConfigOut:
    """查看当前激活(或指定版本)的多智能体编排拓扑、节点详情与上下游连接(只读锁定态)。"""
    await ensure_workflow_config(session)
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
    await ensure_workflow_config(session)
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
            executable=c.runtime_schema_version == RUNTIME_SCHEMA,
            nodes_count=len(c.nodes_json or []),
            edges_count=len(c.edges_json or []),
            change_note=c.change_note,
            created_at=c.created_at,
        )
        for c in cfgs
    ]


@admin_router.post(
    "/workflow/versions/{version}/activate",
    response_model=AgentWorkflowConfigOut,
)
async def activate_workflow_version(
    version: str,
    user: PlatformAdminDep,
    session: SessionDep,
) -> AgentWorkflowConfigOut:
    try:
        target = await activate_version(session, version, user.user_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _to_workflow_out(target)


@admin_router.get("/workflow/export")
async def export_admin_workflow_dsl(
    _: AdminOrReviewerDep,
    session: SessionDep,
    version_no: int | None = Query(default=None),
) -> dict:
    """导出多智能体编排配置包(JSON DSL),供长久维护、跨环境迁移与版本备份。"""
    await ensure_workflow_config(session)
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
    user: PlatformAdminDep,
    session: SessionDep,
) -> AgentWorkflowConfigOut:
    """导入并校验多智能体编排配置包(JSON DSL),生成新版本并保持生产只读安全锁。"""
    await ensure_workflow_config(session)
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
    status_filter: str | None = Query(default=None, alias="status"),
    country_code: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> list[AnalysisTaskSummaryOut]:
    await ensure_workflow_config(session)
    stmt = select(AnalysisTask).where(AnalysisTask.org_id.is_not(None))
    if status_filter:
        stmt = stmt.where(AnalysisTask.status == status_filter)
    if country_code:
        stmt = stmt.where(AnalysisTask.country_code == country_code.upper())
    stmt = stmt.order_by(AnalysisTask.updated_at.desc(), AnalysisTask.created_at.desc())
    tasks = list((await session.execute(stmt)).scalars().all())
    out = [await _to_task_summary(session, t) for t in tasks]
    if keyword and keyword.strip():
        kw = keyword.strip().lower()
        out = [
            item
            for item in out
            if kw in item.conversation_code.lower()
            or kw in str(item.id).lower()
            or kw in item.title.lower()
            or kw in item.vehicle_model.lower()
            or kw in item.org_name.lower()
            or kw in item.creator_email.lower()
            or kw in item.creator_name.lower()
        ]
    return out


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


@admin_router.delete("/tasks/{task_id}")
async def delete_admin_analysis_task(
    task_id: uuid.UUID,
    _: PlatformAdminDep,
    session: SessionDep,
) -> dict:
    """平台管理员删除某条历史对话产生的工作流记录。"""
    task = await session.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话工作流记录不存在")
    code = format_conversation_code(task.id, task.created_at)
    await session.delete(task)
    await session.commit()
    return {"deleted": True, "id": str(task_id), "conversation_code": code}


@admin_router.post("/tasks/{task_id}/rerun", response_model=AnalysisTaskDetailOut)
async def rerun_admin_analysis_task(
    task_id: uuid.UUID,
    _: PlatformAdminDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    """平台管理员重新触发某条已齐备事实的对话工作流。"""
    task = await session.get(AnalysisTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话工作流记录不存在")
    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该对话工作流正在实时运行中",
        )
    if task.sufficiency_score < 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该对话尚未完成八维度必填事实采集,无法启动分析流水线",
        )
    await _start(session, task)
    return await _to_task_detail(session, task)


@admin_router.get("/benchmarks", response_model=BenchmarkSummaryOut)
async def get_admin_benchmarks(
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> BenchmarkSummaryOut:
    await ensure_workflow_config(session)
    res = await run_retrieval_benchmarks(session)
    return BenchmarkSummaryOut(**res)


# ==================== 业务用户端多智能体合规分析与八维度报告接口 ====================


@admin_router.get("/evaluations/suite")
async def get_evaluation_suite(_: AdminOrReviewerDep):
    from app.analysis.evaluation import suite

    return suite()


@admin_router.get("/evaluations/latest")
async def get_latest_evaluation(_: AdminOrReviewerDep, session: SessionDep):
    from app.analysis.evaluation import latest_evaluation

    return await latest_evaluation(session)


@admin_router.post("/evaluations/{batch_id}/cases/{question_id}")
async def execute_evaluation_case(
    batch_id: uuid.UUID,
    question_id: str,
    user: PlatformAdminDep,
    session: SessionDep,
):
    from app.analysis.evaluation import evaluate_case

    try:
        return await evaluate_case(session, question_id, batch_id, user.user_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@user_router.get("/tasks", response_model=list[PublicTaskSummary])
async def list_user_analysis_tasks(
    user: UserDep,
    session: SessionDep,
    country_code: str | None = None,
) -> list[AnalysisTaskSummaryOut]:
    await ensure_workflow_config(session)
    stmt = select(AnalysisTask).where(AnalysisTask.org_id == user.org_id)
    if country_code:
        stmt = stmt.where(AnalysisTask.country_code == country_code.upper())
    stmt = stmt.order_by(AnalysisTask.created_at.asc())
    tasks = list((await session.execute(stmt)).scalars().all())
    return [await _to_task_summary(session, t) for t in tasks]


@user_router.post(
    "/tasks",
    response_model=PublicTaskDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_user_analysis_task(
    body: AnalysisTaskCreateIn,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    await ensure_workflow_config(session)
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
        await _start(session, task)

    return await _to_task_detail(session, task)


@user_router.get("/tasks/{task_id}", response_model=PublicTaskDetail)
async def get_user_analysis_task_detail(
    task_id: uuid.UUID,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None or task.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/facts", response_model=PublicTaskDetail)
async def update_user_task_facts(
    task_id: uuid.UUID,
    body: UpdateFactsIn,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    """补充追问事实；当事实完备度达到 100% 时自动启动多智能体协同生成八维度报告。"""
    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
    )
    if task is None or task.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")

    if task.status == "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="分析正在运行中,请等待完成后再补充事实"
        )
    if task.status == "awaiting_user_facts":
        from app.analysis.user_facts import answer_user_facts

        try:
            resume = await answer_user_facts(session, task, body.facts, body.question_ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if resume and body.auto_run_if_sufficient:
            await _start(session, task)
        return await _to_task_detail(session, task)
    if body.question_ids:
        raise HTTPException(409, "该追问已完成，请勿重复提交旧回答")
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
        await _start(session, task)

    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/run", response_model=PublicTaskDetail)
async def run_user_analysis_task(
    task_id: uuid.UUID,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    task = await session.get(AnalysisTask, task_id)
    if task is None or task.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合规分析任务不存在")
    if task.sufficiency_score < 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必填业务事实尚未补充完整，请先完成追问补充后再启动多智能体分析",
        )
    if task.status == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="分析正在运行中")
    await _start(session, task)
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
    if task is None or task.org_id != user.org_id:
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
        citation_policy=latest.citation_policy,
        task_id=task.id,
        version_no=next_ver,
        status="analysis_failed"
        if latest.status == "analysis_failed"
        else "partial_sample"
        if latest.is_partial_sample
        else "edited",
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
    await session.flush()
    from app.updates.service import link_report, reconcile_impacts

    await link_report(session, task, new_rv)
    await reconcile_impacts(session, task_ids=[task.id])
    await session.commit()
    await session.refresh(new_rv)

    return ReportVersionOut(
        regulatory_updates=await report_updates(session, new_rv.id),
        citation_policy=new_rv.citation_policy or "legacy_unrecorded",
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
    if task is None or task.org_id != user.org_id:
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


@user_router.get("/tasks/{task_id}/reports/{version_no}/export.{ext}")
async def export_user_report(
    task_id: uuid.UUID,
    version_no: int,
    ext: Literal["docx", "pdf", "zip"],
    user: UserDep,
    session: SessionDep,
    dimension: Literal["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"] | None = None,
) -> Response:
    task = await session.get(AnalysisTask, task_id)
    if task is None or task.org_id != user.org_id:
        raise HTTPException(404, "合规分析任务不存在")
    rv = await session.scalar(
        select(ReportVersion).where(
            ReportVersion.task_id == task.id, ReportVersion.version_no == version_no
        )
    )
    if rv is None:
        raise HTTPException(404, "报告版本不存在")
    if rv.status == "analysis_failed":
        raise HTTPException(409, "报告分析技术失败，不能导出为部分样稿，请重试分析")
    if await report_held_citation_ids(session, rv):
        raise HTTPException(409, "报告引用了来源绑定或页级解析待核对的条款，请核查后重新分析再导出")
    if ext == "zip" and dimension:
        raise HTTPException(400, "ZIP 包含整份与八个维度，请勿同时指定单个维度")
    codes = {d["dimension_code"] for d in rv.dimensions_json or []}
    if dimension and dimension not in codes:
        raise HTTPException(404, "该报告版本没有此维度")
    if ext == "zip" and not {f"D{i}" for i in range(1, 9)} <= codes:
        raise HTTPException(409, "历史报告未保存全部八个维度，不能伪造拆分文件")
    export_view = SimpleNamespace(
        version_no=rv.version_no,
        is_partial_sample=rv.is_partial_sample,
        dimensions_json=rv.dimensions_json,
        executive_summary=rv.executive_summary,
        citation_policy=rv.citation_policy,
        regulatory_updates=await report_updates(session, rv.id),
    )
    from starlette.concurrency import run_in_threadpool

    # Native PDF shaping must not block chat progress or SSE heartbeats.
    if ext == "zip":
        data = await run_in_threadpool(build_report_zip, task, export_view)
    elif ext == "docx":
        data = await run_in_threadpool(build_report_docx, task, export_view, dimension)
    else:
        data = await run_in_threadpool(build_report_pdf, task, export_view, dimension)
    media = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
        "zip": "application/zip",
    }
    return Response(
        data,
        media_type=media[ext],
        headers={
            "Content-Disposition": 'attachment; filename="'
            + export_filename(task, rv, ext, dimension)
            + '"',
            "Cache-Control": "private, no-store",
        },
    )


def _event_cursor(last_event_id):
    try:
        value = int(last_event_id or "0")
        if not 0 <= value <= 9223372036854775807:
            raise ValueError
        return value
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID 不合法") from exc


@user_router.get("/tasks/{task_id}/events")
async def user_task_events(
    task_id: uuid.UUID,
    request: Request,
    user: UserDep,
    session: SessionDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    cursor = _event_cursor(last_event_id)
    await validate_reader(session, user, task_id)
    engine = session.bind
    await session.rollback()  # No request transaction/connection held by a long-lived stream.
    return StreamingResponse(
        stream_events(request, engine, user, cursor, task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@admin_router.get("/events")
async def admin_task_events(
    request: Request,
    admin: AdminOrReviewerDep,
    session: SessionDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    cursor = _event_cursor(last_event_id)
    await validate_reader(session, admin)
    if last_event_id is None:
        cursor = await session.scalar(select(func.max(AnalysisEvent.id))) or 0
    engine = session.bind
    await session.rollback()
    return StreamingResponse(
        stream_events(request, engine, admin, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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
    from app.ingestion.safety import document_is_held

    if await document_is_held(session, doc.id):
        raise HTTPException(409, "此引用的原文绑定异常已隔离，请管理员核实来源后重做分析")
    from app.ingestion.layout_safety import layout_held_clause_ids

    layout_pending = bool(await layout_held_clause_ids(session, [clause.id]))
    translation_rows = list(
        (
            await session.execute(
                select(ClauseTranslation)
                .where(
                    ClauseTranslation.clause_id == clause.id,
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status.in_(
                        ("machine_translated", "pending_review", "approved")
                    ),
                    ClauseTranslation.quality_check_passed.is_(True),
                )
                .order_by(ClauseTranslation.updated_at.desc())
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    tr = translation_rows[0] if len(translation_rows) == 1 else None
    translation_ambiguous = len(translation_rows) > 1

    valid_tr_text = (
        tr.translated_text
        if (
            tr is not None
            and tr.review_status not in ("not_translated", "translation_failed", "superseded")
            and (tr.translated_text or "").strip()
        )
        else ""
    )
    from app.knowledge.business_view import topic_cards
    from app.knowledge.legal_terms import current_definitions, term_payload

    return {
        "clause_id": str(clause.id),
        "document_id": str(doc.id),
        "document_title": doc.title,
        "country_code": doc.country_code,
        "clause_number": clause.clause_number or f"#{clause.ordinal}",
        "heading": clause.heading,
        "section_path": clause.section_path,
        "version_no": ver.version_no,
        "source_url": doc.source_url,
        "original_text": clause.original_text,
        "translated_text": valid_tr_text,
        "review_status": "approved"
        if (
            not layout_pending
            and ver.review_status == "approved"
            and not clause.needs_review
            and ((tr and tr.review_status == "approved") or clause.language.startswith("zh"))
        )
        else "pending_review"
        if tr
        else "not_translated",
        "source_integrity_alerts": (
            ["本页存在待核对解析修订；以下仅保留旧原文和译文，禁止作新的法律依据引用。"]
            if layout_pending
            else []
        )
        + (
            ["同一条款存在多个现行译文，尚未消歧；保留原文，不任意选取一个译文。"]
            if translation_ambiguous
            else []
        ),
        "source_review_status": "approved"
        if ver.review_status == "approved" and not clause.needs_review
        else "pending_review",
        "translation_review_status": tr.review_status
        if tr
        else "ambiguous_current"
        if translation_ambiguous
        else "not_translated",
        "usable_as_legal_basis": bool(
            not layout_pending
            and ver.review_status == "approved"
            and not clause.needs_review
            and ((tr and tr.review_status == "approved") or clause.language.startswith("zh"))
        ),
        "related_requirements": await topic_cards(session, doc.country_code, clause_id=clause.id),
        "terms": [
            term_payload(t)
            for t in await current_definitions(session, doc.country_code, doc.id, ver.id)
        ],
    }


@user_router.post("/chats", response_model=PublicTaskDetail, status_code=201)
async def create_chat(user: UserDep, session: SessionDep) -> AnalysisTaskDetailOut:
    """新建一次对话式评估:助手先问候,并开始逐项提问。"""
    task = AnalysisTask(
        org_id=user.org_id,
        created_by=user.user_id,
        title="新对话",
        country_code=PLACEHOLDER_COUNTRY,
        vehicle_model="",
        scenario_type="",
        status="collecting_facts",
        facts_json={},
        missing_facts_json=[],
        sufficiency_score=0,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await start_chat(session, task)
    await session.refresh(task)
    return await _to_task_detail(session, task)


@user_router.post("/tasks/{task_id}/messages", response_model=PublicTaskDetail)
async def post_chat_message(
    task_id: uuid.UUID,
    body: ChatMessageIn,
    user: UserDep,
    session: SessionDep,
) -> AnalysisTaskDetailOut:
    """用户在聊天框发送一条消息:助手记录信息、继续追问,齐备后自动启动分析。"""
    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.id == task_id).with_for_update()
    )
    if task is None or task.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="对话不存在")
    try:
        await post_user_message(
            session, task, body.content, _start, str(body.question_id) if body.question_id else None
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.refresh(task)
    return await _to_task_detail(session, task)


@user_router.get("/benchmarks", response_model=BenchmarkSummaryOut)
async def get_user_benchmarks(
    _: UserDep,
    session: SessionDep,
) -> BenchmarkSummaryOut:
    await ensure_workflow_config(session)
    res = await run_retrieval_benchmarks(session)
    return BenchmarkSummaryOut(**res)
