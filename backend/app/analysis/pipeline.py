"""多智能体合规分析流水线:采集 → 检索 → 分析 → 核验(可退回补证)→ 编制 → 修订联动。

每一步的耗时、Token、工具调用均为真实运行记录;每步完成即落库,前端据此实时展示进度。
没有依据或模型不可用时,如实输出「待核查」,不编造法律结论。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.evidence_selection import (
    AnalysisResponse,
    GroundedClaim,
    select_legal_quote,
    sentence_catalog,
)
from app.analysis.llm import LlmResult, LlmUnavailableError, call_llm_json
from app.analysis.models import AgentWorkflowConfig, AnalysisStepTrace, AnalysisTask, ReportVersion
from app.analysis.retrieval import task_query
from app.analysis.tools import (
    ClauseCandidate,
    ToolBox,
    ToolCall,
    check_country_report_updates,
    verify_finding,
)
from app.analysis.workflow_engine import (
    EIGHT_DIMENSIONS_SPEC,
    evaluate_fact_sufficiency,
)
from app.analysis.workflow_runtime import execution_settings, pin_active_version, require_executable

log = logging.getLogger(__name__)

_RISK_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
_background: set[asyncio.Task] = set()


def launch_pipeline(session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID) -> None:
    """在后台启动流水线(请求立即返回,前端轮询进度)。"""
    t = asyncio.create_task(_guarded_run(session_factory, task_id))
    _background.add(t)
    t.add_done_callback(_background.discard)


async def _guarded_run(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> None:
    async with session_factory() as session:
        try:
            await run_pipeline(session, task_id)
        except Exception as exc:  # noqa: BLE001 - 兜底:任何异常都要把任务标记为失败
            log.exception("分析流水线异常")
            await session.rollback()
            task = await session.get(AnalysisTask, task_id)
            if task is not None:
                task.status = "failed"
                task.updated_at = datetime.now(UTC)
                await _add_trace(
                    session,
                    task,
                    node="node_report_compiler",
                    name="流水线异常终止",
                    phase="error",
                    status="failed",
                    summary=f"流水线异常终止: {type(exc).__name__}: {exc}",
                    calls=[],
                )


async def _add_trace(
    session: AsyncSession,
    task: AnalysisTask,
    *,
    node: str,
    name: str,
    phase: str,
    status: str,
    summary: str,
    calls: list[ToolCall],
    tokens: int = 0,
    duration_ms: int = 0,
    inp: dict[str, Any] | None = None,
    out: dict[str, Any] | None = None,
    is_backtrack: bool = False,
    reason: str | None = None,
    resolution: str | None = None,
) -> AnalysisStepTrace:
    seq = (
        len(
            (
                await session.execute(
                    select(AnalysisStepTrace.id).where(AnalysisStepTrace.task_id == task.id)
                )
            )
            .scalars()
            .all()
        )
        + 1
    )
    inp = dict(inp or {})
    if task.workflow_version_id:
        cfg = await session.get(AgentWorkflowConfig, task.workflow_version_id)
        inp["workflow_execution"] = {
            "workflow_version_id": str(cfg.id),
            "version_no": cfg.version_no,
            "node_settings": execution_settings(
                next(n for n in cfg.nodes_json if n["node_id"] == node)
            ),
        }
    tr = AnalysisStepTrace(
        task_id=task.id,
        workflow_version_id=task.workflow_version_id,
        seq_no=seq,
        agent_node_id=node,
        agent_name=name,
        phase=phase,
        status=status,
        business_summary=summary,
        is_backtrack=is_backtrack,
        backtrack_reason=reason,
        backtrack_resolution=resolution,
        tool_calls_json=[c.as_dict() for c in calls],
        input_snapshot_json=inp or {},
        output_snapshot_json=out or {},
        tokens_used=tokens,
        duration_ms=duration_ms,
    )
    session.add(tr)
    task.total_tokens = (task.total_tokens or 0) + tokens
    task.total_duration_ms = (task.total_duration_ms or 0) + duration_ms
    task.mcp_calls_count = (task.mcp_calls_count or 0) + sum(
        1 for c in calls if not c.tool_name.startswith("llm.")
    )
    task.updated_at = datetime.now(UTC)
    await session.commit()
    return tr


def _candidate_payload(c: ClauseCandidate) -> dict[str, str]:
    return {
        "clause_id": c.clause_id,
        "document": c.document_title,
        "clause_number": c.label,
        "sentences": sentence_catalog(c.text),
    }


def _build_user_message(
    task: AnalysisTask,
    dim_code: str,
    dim_title: str,
    cands: list[ClauseCandidate],
    feedback: list[str] | None,
) -> str:
    schema = AnalysisResponse.model_json_schema()
    properties = schema["$defs"]["FindingSelection"]["properties"]
    properties["fact_key"]["enum"] = list(task.facts_json or {})
    properties["clause_id"]["enum"] = [c.clause_id for c in cands]
    payload: dict[str, Any] = {
        "phase": "select_evidence",
        "country": task.country_code,
        "vehicle_model": task.vehicle_model,
        "scenario": task.scenario_type,
        "business_facts": task.facts_json or {},
        "question": task_query(task.facts_json or {}, dim_code),
        "dimension": {"code": dim_code, "title": dim_title},
        "candidate_clauses": [_candidate_payload(c) for c in cands],
        "output_schema": schema,
    }
    if feedback:
        payload["verifier_feedback_must_fix"] = feedback
    return json.dumps(payload, ensure_ascii=False)


_GROUNDING_GATE = asyncio.Semaphore(6)


async def _ground_selection(task, dim_code, dim_title, finding, candidates, config):
    """One isolated evidence unit per generation. Other clauses/draft rules are not sent."""
    result = dict(finding)
    # A selection-stage draft is not a legal conclusion.
    result["obligation"] = ""
    result["quote_zh"] = ""
    candidate = next((c for c in candidates if c.clause_id == finding["clause_id"]), None)
    if candidate is None:
        return result, {"requests": 0, "tokens": 0, "error": "clause_id不在检索结果中"}
    try:
        result.update(select_legal_quote(candidate.text, finding["sentence_ids"]))
    except ValueError as exc:
        return result, {"requests": 0, "tokens": 0, "error": str(exc)}
    message = {
        "phase": "write_from_evidence",
        "dimension": {"code": dim_code, "title": dim_title},
        "business_fact": str(task.facts_json[finding["fact_key"]]),
        "question": task_query(task.facts_json or {}, dim_code),
        "evidence": {"quote": result["quote"]},
        "output_schema": GroundedClaim.model_json_schema(),
        "fidelity_checklist": [
            "主体、代词和所有格必须相同，不能新增另一主体",
            "规范性动词、行为对象、范围和全部例外保持一致",
            "条文引用保留原文层级标记，不猜译法律层级",
            "申报/许可/授权等术语有歧义时保留原词，不能自行认定性质",
            "完整条件来自原文，核验进度或缺其他材料不是本条法律条件",
        ],
    }
    # The model sees the same exclusive rule/gap contract that Pydantic enforces.
    message["output_schema"]["oneOf"] = [
        {
            "required": ["obligation", "gap"],
            "properties": {"obligation": {"minLength": 1}, "gap": {"const": ""}},
        },
        {
            "required": ["obligation", "gap"],
            "properties": {"obligation": {"const": ""}, "gap": {"minLength": 1}},
        },
    ]
    tokens = requests = 0
    failures = []
    for attempt in range(2):
        try:
            async with _GROUNDING_GATE:
                response = await call_llm_json(
                    [
                        {"role": "system", "content": config["system_prompt"]},
                        {"role": "user", "content": json.dumps(message, ensure_ascii=False)},
                    ],
                    model=config["model"],
                    temperature=config["temperature"],
                    timeout=config["timeout"],
                    max_tokens=2000,
                )
            tokens += response.total_tokens
            requests += response.request_attempts
            failures.extend(response.failures)
            claim = GroundedClaim.model_validate(response.data)
            result.update(obligation=claim.obligation, risk_level=claim.risk_level)
            if claim.gap:
                result["_generation_gap"] = claim.gap
            else:
                ok, issues = verify_finding(result, candidate.text)
                if not ok:
                    raise ValueError("；".join(issues))
            return result, {
                "requests": requests,
                "tokens": tokens,
                "failures": failures,
                "gap": claim.gap,
            }
        except LlmUnavailableError as exc:
            tokens += exc.tokens
            requests += len(exc.failures)
            failures.extend(exc.failures)
            result["_generation_error"] = str(exc)
            break
        except ValueError as exc:
            detail = (
                str(exc.errors(include_input=False, include_url=False))
                if hasattr(exc, "errors")
                else str(exc)
            )
            failures.append({"kind": "grounding_validation_error", "reason": detail[:1600]})
            message["schema_feedback"] = detail[:1600]
            if attempt == 1:
                result["_generation_error"] = "依据限定生成校验失败: " + detail[:1600]
    return result, {
        "requests": requests,
        "tokens": tokens,
        "failures": failures,
        "error": result.get("_generation_error"),
    }


async def _analyse_dimension(
    task: AnalysisTask,
    dim_code: str,
    dim_title: str,
    cands: list[ClauseCandidate],
    feedback: list[str] | None,
    config: dict[str, Any],
    *,
    _attempt_budget: int = 2,
) -> tuple[dict[str, Any], ToolCall]:
    """单个维度的分析调用;模型不可用时返回带 error 的结果而不是编造内容。"""
    msgs = [
        {"role": "system", "content": config["system_prompt"]},
        {
            "role": "user",
            "content": _build_user_message(task, dim_code, dim_title, cands, feedback),
        },
    ]
    started = time.perf_counter()
    attempts = 0
    used_tokens = 0
    failures = []
    request_attempts = 0
    grounding_ledger = []
    error_kind = "model_unavailable"
    try:
        async with asyncio.timeout(config["timeout"]):
            for attempts in range(1, _attempt_budget + 1):
                res: LlmResult = await call_llm_json(
                    msgs,
                    model=config["model"],
                    temperature=config["temperature"],
                    timeout=config["timeout"],
                )
                used_tokens += res.total_tokens
                request_attempts += res.request_attempts
                failures.extend(res.failures)
                try:
                    validated = AnalysisResponse.model_validate(res.data)
                    if any(f.fact_key not in (task.facts_json or {}) for f in validated.findings):
                        raise ValueError("fact_key 不在用户已提供事实中")
                    break
                except ValueError as exc:
                    error_kind = "model_format_error"
                    detail = (
                        str(exc.errors(include_input=False, include_url=False))[:1800]
                        if hasattr(exc, "errors")
                        else str(exc)[:1800]
                    )
                    failures.append({"kind": "model_schema_error", "reason": detail})
                    if attempts == _attempt_budget:
                        raise LlmUnavailableError(
                            "模型JSON Schema/事实键校验连续失败: " + detail
                        ) from exc
                    msgs[1]["content"] = _build_user_message(
                        task,
                        dim_code,
                        dim_title,
                        cands,
                        [
                            *(feedback or []),
                            "格式错误：只返回指定Schema，fact_key与句子编号取自输入，不要输出quote。",
                            detail,
                        ],
                    )

            async def ground_one(finding):
                nonlocal used_tokens, request_attempts
                finding, entry = await _ground_selection(
                    task,
                    dim_code,
                    dim_title,
                    finding.model_dump(),
                    cands,
                    config,
                )
                used_tokens += entry["tokens"]
                request_attempts += entry["requests"]
                grounding_ledger.append(
                    {
                        **entry,
                        "clause_id": finding["clause_id"],
                        "sentence_ids": finding["sentence_ids"],
                    }
                )
                return finding

            grounded = await asyncio.gather(*[ground_one(f) for f in validated.findings])
            refused = [f for f in grounded if f.get("_generation_gap")]
            if refused and attempts < _attempt_budget:
                # Reuse the existing two-selection-attempt budget, not a new retry loop.
                # A source-limited refusal goes back to the selector with its actual reason.
                # Preserve every first proposal/quote, even when a later selection succeeds.
                revised, call = await _analyse_dimension(
                    task,
                    dim_code,
                    dim_title,
                    cands,
                    [
                        *(feedback or []),
                        "上次所选证据无法支持完整规则，请重新选择完整依据；不能补字或放宽核验。",
                        *[f["clause_id"] + ": " + f["_generation_gap"] for f in refused],
                    ],
                    config,
                    _attempt_budget=_attempt_budget - attempts,
                )
                revised["tokens"] += used_tokens
                call.arguments.update(
                    retry=bool(feedback),
                    schema_attempts=attempts + call.arguments["schema_attempts"],
                    request_attempts=request_attempts + call.arguments["request_attempts"],
                    failures=failures + call.arguments.get("failures", []),
                    grounding_calls=grounding_ledger + call.arguments["grounding_calls"],
                    selection_retries=[{"reason": "source_limited_refusal", "findings": grounded}],
                )
                call.duration_ms = int((time.perf_counter() - started) * 1000)
                call.result_summary += (
                    f"；含保留原证据的局部重选，合计已知Token {revised['tokens']}"
                )
                return revised, call
    except (LlmUnavailableError, TimeoutError) as exc:
        used_tokens += getattr(exc, "tokens", 0)
        failures.extend(getattr(exc, "failures", []))
        if error_kind != "model_format_error":
            error_kind = getattr(exc, "kind", "model_timeout")
        call = ToolCall(
            call_id=f"llm-{dim_code}",
            tool_name="llm.chat",
            tool_label=f"{dim_code} 维度分析(模型调用)",
            arguments={
                "dimension": dim_code,
                "candidates": len(cands),
                "schema_attempts": attempts,
                "error_kind": error_kind,
                "failures": failures,
                "request_attempts": request_attempts + len(getattr(exc, "failures", [])),
                "grounding_calls": grounding_ledger,
            },
            result_summary=str(exc) or "模型调用超过配置时限",
            duration_ms=int((time.perf_counter() - started) * 1000),
            verified=False,
        )
        return {
            "error": str(exc) or "模型调用超过配置时限",
            "error_kind": error_kind,
            "tokens": used_tokens,
            "findings": [],
        }, call
    data = validated.model_dump()
    data["evidence_selections"] = [
        {"clause_id": f.clause_id, "sentence_ids": f.sentence_ids, "fact_key": f.fact_key}
        for f in validated.findings
    ]
    data["findings"] = grounded
    for finding in data["findings"]:
        finding["business_fact"] = str(task.facts_json[finding["fact_key"]])
        finding["quote_zh"] = ""
    data.update(tokens=used_tokens, model=res.model)

    call = ToolCall(
        call_id=f"llm-{dim_code}",
        tool_name="llm.chat",
        tool_label=f"{dim_code} 维度分析(模型 {res.model})",
        arguments={
            "dimension": dim_code,
            "candidates": len(cands),
            "retry": bool(feedback),
            "schema_attempts": attempts,
            "failures": failures,
            "request_attempts": request_attempts,
            "grounding_calls": grounding_ledger,
            "model": config["model"],
            "temperature": config["temperature"],
            "timeout": config["timeout"],
        },
        result_summary=(
            f"选证据后独立生成 {len(data['findings'])} 项规则/缺口；已知Token {used_tokens}"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        verified=not any(entry.get("error") or entry.get("gap") for entry in grounding_ledger),
    )
    return data, call


def _verify_dimension(
    dim_code: str, result: dict[str, Any], cands: list[ClauseCandidate]
) -> list[str]:
    """核验一个维度的全部结论,把通过/失败写回 finding,并返回问题清单。"""
    by_id = {c.clause_id: c for c in cands}
    problems: list[str] = []
    if cands and (result.get("error") or not result.get("findings")):
        detail = result.get("error") or result.get("gaps") or "没有结论且没有具体原因"
        problems.append(f"{dim_code}: 有候选但未形成可核验结论：{detail}")
    for f in result.get("findings", []):
        if not isinstance(f, dict):
            continue
        cand = by_id.get(str(f.get("clause_id")))
        if cand is None:
            f["_ok"] = False
            f["_issues"] = ["clause_id 不在检索结果中"]
            problems.append(f"{dim_code}: 引用了检索结果之外的条款")
            continue
        try:
            f.update(
                select_legal_quote(
                    cand.text, f.get("selected_sentence_ids") or f.get("sentence_ids") or []
                )
            )
            if f.get("_generation_error") or f.get("_generation_gap"):
                ok, issues = False, [f.get("_generation_error") or f["_generation_gap"]]
            else:
                ok, issues = verify_finding(f, cand.text)
        except ValueError as exc:
            ok, issues = False, [str(exc)]
        f["_ok"] = ok
        f["_issues"] = issues
        if not ok:
            problems.append(
                f"{dim_code} {cand.document_title[:30]} {cand.label}: " + ";".join(issues)
            )
    return problems


async def run_pipeline(session: AsyncSession, task_id: uuid.UUID) -> None:
    task = await session.get(AnalysisTask, task_id)
    if task is None:
        return
    cc = task.country_code.upper()
    cfg = (
        await session.get(AgentWorkflowConfig, task.workflow_version_id)
        if task.workflow_version_id
        else None
    )
    if cfg is None:
        cfg = await pin_active_version(session, task)
    require_executable(cfg)
    runtime = {n["node_id"]: execution_settings(n) for n in cfg.nodes_json}
    task.status = "running"
    task.total_tokens = task.total_duration_ms = task.mcp_calls_count = 0
    task.backtrack_count = 0
    task.findings_json = []
    await session.commit()

    tb = ToolBox(session, cc)

    # ---- 1. 信息采集与追问 ----
    t0 = time.perf_counter()
    score, missing = evaluate_fact_sufficiency(cc, task.facts_json or {})
    pending = sum(1 for m in missing if not m["is_fulfilled"])
    task.sufficiency_score = score
    task.missing_facts_json = missing
    tb._record(
        "validate_fact_schema",
        "八维度事实完备度校验",
        {"country_code": cc, "required": len(missing)},
        f"完备度 {score}%,待补充 {pending} 项",
        t0,
        verified=pending == 0,
    )
    await _add_trace(
        session,
        task,
        node="node_fact_collector",
        name="信息采集与追问 Agent",
        phase="fact_check",
        status="passed" if pending == 0 else "failed",
        summary=f"事实完备度核验:{len(missing) - pending}/{len(missing)} 项必填事实已采集"
        f"(完备度 {score}%)",
        calls=list(tb.calls),
        duration_ms=tb.calls[-1].duration_ms,
        inp={
            "country_code": cc,
            "vehicle_model": task.vehicle_model,
            "scenario_type": task.scenario_type,
            "required_slots_count": len(missing),
        },
        out={
            "sufficiency_score": score,
            "missing": pending,
            "collected_facts": dict(task.facts_json or {}),
            "fact_check_items": [
                {
                    "fact_key": m["fact_key"],
                    "dimension_code": m["dimension_code"],
                    "question_zh": m["question_zh"],
                    "value": m.get("current_value") or "",
                    "is_fulfilled": bool(m.get("is_fulfilled")),
                }
                for m in missing
            ],
        },
    )
    if pending:
        task.status = "collecting_facts"
        await session.commit()
        return

    # ---- 2. 检索 ----
    mark = len(tb.calls)

    async def retrieve():
        availability = await tb.check_source_availability()
        # check_source_availability populates the shared source cache first. The search
        # calls below read that cache; no concurrent commands use the AsyncSession.
        searched = await asyncio.gather(
            *[
                tb.search_effective_clauses(
                    code,
                    top_k=runtime["node_mcp_retriever"]["top_k"],
                    query=task_query(task.facts_json or {}, code),
                    config=runtime["node_mcp_retriever"],
                )
                for code, _ in EIGHT_DIMENSIONS_SPEC
            ]
        )
        dim_cands = dict(zip((c for c, _ in EIGHT_DIMENSIONS_SPEC), searched, strict=True))
        all_cands = {c.clause_id: c for lst in dim_cands.values() for c in lst}
        await tb.query_kg_obligations(list(all_cands))
        await tb.verify_clause_effective_date(list(all_cands.values()))
        return availability, dim_cands

    retrieval_started = time.perf_counter()
    availability, dim_cands = await asyncio.wait_for(
        retrieve(),
        timeout=runtime["node_mcp_retriever"]["timeout"],
    )
    step_calls = tb.calls[mark:]
    covered = sum(1 for v in dim_cands.values() if v)
    await _add_trace(
        session,
        task,
        node="node_mcp_retriever",
        name="MCP 法规混合检索 Agent",
        phase="retrieval",
        status="passed" if covered == 8 else "warning",
        summary=(
            f"在{cc}可检索法源中为 8 个维度检索候选条款:{covered}/8 个维度有候选条款;"
            f"可用条款 {availability['clauses_usable']}/{availability['clauses_total']} 条。"
            "现行状态未经官方核实的版本不视为已确认现行有效。"
        ),
        calls=step_calls,
        tokens=tb.retrieval_tokens,
        duration_ms=int((time.perf_counter() - retrieval_started) * 1000),
        inp={
            "country_code": cc,
            "dimensions": [code for code, _ in EIGHT_DIMENSIONS_SPEC],
        },
        out={
            "source_availability": availability,
            "candidates_per_dimension": {k: len(v) for k, v in dim_cands.items()},
            "retrieved_clauses_by_dimension": {
                code: [
                    {
                        "clause_id": c.clause_id,
                        "document_title": c.document_title,
                        "clause_number": c.label,
                        "version_no": c.version_no,
                        "review_status": c.review_status,
                        "snippet": c.text[:260],
                    }
                    for c in cands
                ]
                for code, cands in dim_cands.items()
            },
        },
    )

    # ---- 3. 分析(8 个维度并行调用模型) ----
    dim_titles = dict(EIGHT_DIMENSIONS_SPEC)
    results: dict[str, dict[str, Any]] = {}
    active = [code for code, _ in EIGHT_DIMENSIONS_SPEC if dim_cands[code]]
    t_start = asyncio.get_event_loop().time()
    outs = await asyncio.gather(
        *[
            _analyse_dimension(
                task, c, dim_titles[c], dim_cands[c], None, runtime["node_dimension_analyst"]
            )
            for c in active
        ]
    )
    wall = int((asyncio.get_event_loop().time() - t_start) * 1000)
    llm_calls: list[ToolCall] = []
    for code, (data, call) in zip(active, outs, strict=True):
        results[code] = data
        llm_calls.append(call)
    tokens = sum(int(r.get("tokens") or 0) for r in results.values())
    errors = [c for c in llm_calls if not c.verified]
    await _add_trace(
        session,
        task,
        node="node_dimension_analyst",
        name="八维度合规矩阵分析 Agent",
        phase="analysis",
        status="failed"
        if errors and len(errors) == len(llm_calls)
        else "warning"
        if errors
        else "passed",
        summary=(
            f"对 {len(active)} 个有候选条款的维度调用模型分析,共得到 "
            f"{sum(len(r.get('findings', [])) for r in results.values())} 条待核验结论"
            + (f";{len(errors)} 个维度模型/依据生成未通过,如实标记为待核查" if errors else "")
        ),
        calls=llm_calls,
        tokens=tokens,
        duration_ms=wall,
        inp={
            "country_code": cc,
            "vehicle_model": task.vehicle_model,
            "scenario_type": task.scenario_type,
            "dimensions_requested": active,
        },
        out={
            "dimensions_analysed": active,
            "total_findings_drafted": sum(len(r.get("findings", [])) for r in results.values()),
            "dimension_outputs": {
                code: {
                    "dimension_title": dim_titles.get(code, code),
                    "model": r.get("model", ""),
                    "tokens": r.get("tokens", 0),
                    "summary": r.get("summary", ""),
                    "findings_count": len(r.get("findings", [])),
                    "evidence_selections": r.get("evidence_selections", []),
                    "findings": [
                        {
                            "business_fact": str(f.get("business_fact") or ""),
                            "clause_id": str(f.get("clause_id") or ""),
                            "obligation": str(f.get("obligation") or ""),
                            "risk_level": str(f.get("risk_level") or "MEDIUM"),
                            "quote": str(f.get("quote") or ""),
                            "sentence_ids": f.get("sentence_ids") or [],
                            "quote_zh": str(f.get("quote_zh") or ""),
                        }
                        for f in r.get("findings", [])
                        if isinstance(f, dict)
                    ],
                    "recommendations": list(r.get("recommendations") or []),
                    "gaps": str(r.get("gaps") or ""),
                    "error": r.get("error"),
                }
                for code, r in results.items()
            },
        },
    )

    # ---- 4. 核验(程序化子串/数字校验) ----
    async def _verify_round(codes: list[str], phase: str) -> list[str]:
        v0 = asyncio.get_event_loop().time()
        calls: list[ToolCall] = []
        problems: list[str] = []
        verification_items: list[dict[str, Any]] = []
        readiness_items: list[dict[str, Any]] = []
        for code in codes:
            r = results.get(code) or {}
            c0 = asyncio.get_event_loop().time()
            probs = _verify_dimension(code, r, dim_cands[code])
            readiness_items.append(
                {
                    "dimension_code": code,
                    "has_candidates": bool(dim_cands[code]),
                    "has_findings": bool(r.get("findings")),
                    "error": r.get("error"),
                    "gap": r.get("gaps"),
                    "passed": bool(r.get("findings")) and not r.get("error") and not probs,
                }
            )
            by_id = {c.clause_id: c for c in dim_cands[code]}
            for f in r.get("findings", []):
                if not isinstance(f, dict):
                    continue
                cand = by_id.get(str(f.get("clause_id")))
                verification_items.append(
                    {
                        "dimension_code": code,
                        "clause_id": str(f.get("clause_id") or ""),
                        "document_title": cand.document_title if cand else "未匹配法源",
                        "clause_number": cand.label if cand else "-",
                        "obligation": str(f.get("obligation") or ""),
                        "quote": str(f.get("quote") or ""),
                        "passed": bool(f.get("_ok")),
                        "sentence_ids": f.get("sentence_ids") or [],
                        "quote_start": f.get("quote_start"),
                        "quote_end": f.get("quote_end"),
                        "source_text_sha256": f.get("source_text_sha256"),
                        "selected_sentence_ids": f.get("selected_sentence_ids", []),
                        "anchor_quote_start": f.get("anchor_quote_start"),
                        "anchor_quote_end": f.get("anchor_quote_end"),
                        "issues": list(f.get("_issues") or []),
                    }
                )
            n = len(r.get("findings", []))
            calls.append(
                ToolCall(
                    call_id=f"verify-{phase}-{code}",
                    tool_name="verify_quote_substring",
                    tool_label=f"{code} 引用子串与数字核验",
                    arguments={"dimension": code, "findings": n},
                    result_summary=(
                        f"{sum(bool(f.get('_ok')) for f in r.get('findings', []))}/{n} 条通过"
                        + (f";{'; '.join(probs)}" if probs else "")
                    ),
                    duration_ms=int((asyncio.get_event_loop().time() - c0) * 1000),
                    verified=bool(n) and not probs,
                )
            )
            problems.extend(probs)
        ms = int((asyncio.get_event_loop().time() - v0) * 1000)
        await _add_trace(
            session,
            task,
            node="node_citation_verifier",
            name="依据子串核验与退回质检 Agent",
            phase=phase,
            status="backtracked"
            if problems
            else "passed"
            if verification_items
            else "not_evaluated",
            summary=(
                f"核验未通过 {len(problems)} 条,退回分析 Agent 补充依据"
                if problems
                else "全部结论的引用均为原文连续子串，数字与所选引文一致"
                if verification_items
                else "无可核验结论，不能记作通过"
            ),
            calls=calls,
            duration_ms=ms,
            is_backtrack=bool(problems),
            reason="\n".join(problems) if problems else None,
            inp={"dimensions_checked": codes, "phase": phase},
            out={
                "verified_passed_count": sum(1 for x in verification_items if x["passed"]),
                "verified_failed_count": len(problems),
                "problems": problems,
                "verification_items": verification_items,
                "readiness_items": readiness_items,
            },
        )
        return problems

    problems = await _verify_round(list(results), "verification")
    for round_no in range(1, runtime["node_citation_verifier"]["max_backtrack_rounds"] + 1):
        if not problems:
            break
        task.backtrack_count = round_no
        bad_codes = sorted({p.split(":")[0].split(" ")[0] for p in problems})
        b0 = asyncio.get_event_loop().time()
        outs2 = await asyncio.gather(
            *[
                _analyse_dimension(
                    task,
                    c,
                    dim_titles[c],
                    dim_cands[c],
                    [p for p in problems if p.startswith(c)],
                    runtime["node_dimension_analyst"],
                )
                for c in bad_codes
            ]
        )
        calls2: list[ToolCall] = []
        for code, (data, call) in zip(bad_codes, outs2, strict=True):
            results[code] = data
            calls2.append(call)
        tokens2 = sum(int(d.get("tokens") or 0) for d, _ in outs2)
        redo = await _add_trace(
            session,
            task,
            node="node_dimension_analyst",
            name="八维度合规矩阵分析 Agent",
            phase="re_analysis",
            status="failed" if any(results[c].get("error") for c in bad_codes) else "passed",
            summary=f"按质检退回意见对 {len(bad_codes)} 个维度重新分析并补充依据",
            calls=calls2,
            tokens=tokens2,
            duration_ms=int((asyncio.get_event_loop().time() - b0) * 1000),
            is_backtrack=True,
            reason="\n".join(problems),
            inp={
                "re_analysed_dimensions": bad_codes,
                "verifier_feedback": problems,
            },
            out={
                "dimensions_re_analysed": bad_codes,
                "dimension_outputs": {
                    code: {
                        "dimension_title": dim_titles.get(code, code),
                        "model": results[code].get("model", ""),
                        "tokens": results[code].get("tokens", 0),
                        "summary": results[code].get("summary", ""),
                        "findings_count": len(results[code].get("findings", [])),
                        "evidence_selections": results[code].get("evidence_selections", []),
                        "findings": [
                            {
                                "business_fact": str(f.get("business_fact") or ""),
                                "clause_id": str(f.get("clause_id") or ""),
                                "obligation": str(f.get("obligation") or ""),
                                "risk_level": str(f.get("risk_level") or "MEDIUM"),
                                "quote": str(f.get("quote") or ""),
                                "sentence_ids": f.get("sentence_ids") or [],
                                "quote_zh": str(f.get("quote_zh") or ""),
                            }
                            for f in results[code].get("findings", [])
                            if isinstance(f, dict)
                        ],
                        "recommendations": list(results[code].get("recommendations") or []),
                        "gaps": str(results[code].get("gaps") or ""),
                        "error": results[code].get("error"),
                    }
                    for code in bad_codes
                    if code in results
                },
            },
        )
        problems2 = await _verify_round(bad_codes, "re_verification")
        redo.backtrack_resolution = (
            "补证模型调用或格式校验失败，不能记作核验通过"
            if any(results[c].get("error") for c in bad_codes)
            else f"第 {round_no + 1} 次核验全部通过"
            if not problems2
            else f"第 {round_no + 1} 次核验仍有 {len(problems2)} 条未通过，按配置补证或剔除"
        )
        await session.commit()
        problems = problems2

    # ---- 5. 编制 ----
    rv, cited_versions, full = await asyncio.wait_for(
        _compile(session, task, tb, results, dim_cands, dim_titles),
        timeout=runtime["node_report_compiler"]["timeout"],
    )
    await _update_linkage(session, task, rv, cited_versions, full, runtime)


async def _compile(
    session: AsyncSession,
    task: AnalysisTask,
    tb: ToolBox,
    results: dict[str, dict[str, Any]],
    dim_cands: dict[str, list[ClauseCandidate]],
    dim_titles: dict[str, str],
) -> tuple[ReportVersion, dict[str, set[str]], bool]:
    c0 = asyncio.get_event_loop().time()
    dimensions: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    cited_versions: dict[str, set[str]] = {}
    fid = 0
    for code, title in EIGHT_DIMENSIONS_SPEC:
        r = results.get(code) or {}
        by_id = {c.clause_id: c for c in dim_cands.get(code, [])}
        good = [f for f in r.get("findings", []) if isinstance(f, dict) and f.get("_ok")]
        dropped = len(r.get("findings", [])) - len(good)
        citations: list[dict[str, Any]] = []
        lines: list[str] = []
        for idx, f in enumerate(good, start=1):
            cand = by_id[str(f["clause_id"])]
            tag = f"[{code}-C{idx}]"
            fid += 1
            risk = str(f.get("risk_level") or "MEDIUM").upper()
            risk = risk if risk in _RISK_ORDER else "MEDIUM"
            f["risk_level"] = risk
            citations.append(
                {
                    "citation_tag": tag,
                    "clause_id": cand.clause_id,
                    "document_id": cand.document_id,
                    "document_title": cand.document_title,
                    "clause_number": cand.label,
                    "version_no": cand.version_no,
                    "source_url": cand.source_url,
                    "quote_original": str(f["quote"]),
                    "quote_zh": str(f.get("quote_zh") or "(未提供译文)"),
                    "review_status": cand.review_status,
                    **{
                        key: f.get(key)
                        for key in (
                            "sentence_ids",
                            "quote_start",
                            "quote_end",
                            "source_text_sha256",
                            "selection_version",
                            "selected_sentence_ids",
                            "anchor_quote_start",
                            "anchor_quote_end",
                        )
                    },
                }
            )
            lines.append(f"- {tag} {f.get('obligation', '')}(风险:{risk})")
            cited_versions.setdefault(cand.version_id, set()).add(code)
            findings.append(
                {
                    "finding_id": f"F-{fid:02d}",
                    "dimension_code": code,
                    "business_fact": str(f.get("business_fact") or ""),
                    "clause_id": cand.clause_id,
                    "document_id": cand.document_id,
                    "document_title": cand.document_title,
                    "clause_number": cand.label,
                    "version_no": cand.version_no,
                    "source_url": cand.source_url,
                    "compliance_obligation": str(f.get("obligation") or ""),
                    "risk_level": risk,
                    "basis_quote": str(f["quote"]),
                    "basis_zh": str(f.get("quote_zh") or ""),
                    "verification_status": "VERIFIED_SUBSTRING",
                    **{
                        key: f.get(key)
                        for key in (
                            "sentence_ids",
                            "quote_start",
                            "quote_end",
                            "source_text_sha256",
                            "selection_version",
                            "selected_sentence_ids",
                            "anchor_quote_start",
                            "anchor_quote_end",
                        )
                    },
                }
            )
        gap: str | None = None
        if r.get("error"):
            gap = "分析技术失败（模型调用或格式错误），请重试；不能当作法源不足"
        elif not dim_cands.get(code):
            gap = "已归档法源中未检索到与该维度相关的可用条款(待核查)"
        elif not good:
            gap = r.get("gaps") or "模型未能给出可通过原文核验的结论(待核查)"
        elif dropped:
            gap = f"另有 {dropped} 条结论未通过原文核验,已剔除(待核查)"
        technical_failure = bool(r.get("error") or dropped or (dim_cands.get(code) and not good))
        if technical_failure and not gap:
            gap = "证据选择/核验仍失败，未生成可交付结论；请重试分析"
        body = ""
        if lines:
            body += "依据与合规义务(均已通过原文子串/数字核验,条款状态为机器翻译待律所复核):\n"
            body += "\n".join(lines)
        elif gap:
            body = "本维度暂无经核验的依据,结论待核查。"
        levels = [str(f["risk_level"]) for f in good]
        dimensions.append(
            {
                "dimension_code": code,
                "dimension_title": title,
                "status": "ANALYSIS_ERROR"
                if technical_failure
                else "COMPLETE"
                if good and not gap
                else "PARTIAL_GAP",
                "gap_kind": "analysis_error"
                if technical_failure
                else "source_gap"
                if gap
                else None,
                "risk_level": max(levels, key=lambda x: _RISK_ORDER[x])
                if levels
                else "PENDING_VERIFICATION",
                "content_markdown": body,
                "key_recommendations": [
                    "核对并落实：" + str(f.get("obligation") or "") for f in good
                ],
                "citations": citations,
                "missing_gap_note": gap,
            }
        )
    technical_failed = any(d["status"] == "ANALYSIS_ERROR" for d in dimensions)
    covered = sum(1 for d in dimensions if d["citations"])
    full = all(d["status"] == "COMPLETE" for d in dimensions)
    summary = (
        f"本报告针对【{task.vehicle_model}】在【{task.country_code}】的业务场景,"
        f"由多智能体检索条款、模型分析并经程序核验:共 {len(findings)} 条结论通过原文核验,"
        f"覆盖 {covered}/8 个维度。所有引用均为官方原文,中文为机器翻译,尚未经律所复核。"
        + (
            "分析存在技术失败，当前记录不可作为报告交付，请检查失败原因后重试。"
            if technical_failed
            else ""
            if full
            else "尚有维度缺少可用法源,已逐维标注证据缺口,本报告为部分样稿。"
        )
    )
    rvs = list(
        (await session.execute(select(ReportVersion).where(ReportVersion.task_id == task.id)))
        .scalars()
        .all()
    )
    rv = ReportVersion(
        task_id=task.id,
        version_no=max((r.version_no for r in rvs), default=0) + 1,
        status="analysis_failed"
        if technical_failed
        else "full_draft"
        if full
        else "partial_sample",
        is_partial_sample=not full and not technical_failed,
        change_note="多智能体协同自动编制(引用均经原文子串与数字核验)",
        executive_summary=summary,
        dimensions_json=dimensions,
        stale_impacts_json=[],
        created_by=task.created_by,
    )
    session.add(rv)
    task.findings_json = findings
    comp = ToolCall(
        call_id="compile-1",
        tool_name="compile_report_version",
        tool_label="生成不可变八维度报告版本",
        arguments={"dimensions": 8, "verified_findings": len(findings)},
        result_summary=f"版本 v{rv.version_no};覆盖 {covered}/8 个维度",
        duration_ms=int((asyncio.get_event_loop().time() - c0) * 1000),
    )
    await _add_trace(
        session,
        task,
        node="node_report_compiler",
        name="八维度国别报告编制 Agent",
        phase="compilation",
        status="failed" if technical_failed else "passed" if full else "warning",
        summary=(
            f"编制完成:{len(findings)} 条核验通过的结论,{covered}/8 个维度有依据"
            + ("" if full else ";缺依据的维度已标注缺口,报告为部分样稿")
        ),
        calls=[comp],
        duration_ms=comp.duration_ms,
        inp={
            "country_code": task.country_code,
            "vehicle_model": task.vehicle_model,
            "verified_findings_count": len(findings),
        },
        out={
            "report_version": rv.version_no,
            "report_status": rv.status,
            "is_partial_sample": rv.is_partial_sample,
            "executive_summary": summary,
            "verified_findings_count": len(findings),
            "covered_dimensions_count": covered,
            "dimensions_deliverables": [
                {
                    "dimension_code": d["dimension_code"],
                    "dimension_title": d["dimension_title"],
                    "status": d["status"],
                    "risk_level": d["risk_level"],
                    "citations_count": len(d["citations"]),
                    "content_markdown": d["content_markdown"],
                    "key_recommendations": d["key_recommendations"],
                    "missing_gap_note": d["missing_gap_note"],
                }
                for d in dimensions
            ],
        },
    )

    return rv, cited_versions, full


async def _update_linkage(
    session: AsyncSession,
    task: AnalysisTask,
    rv: ReportVersion,
    cited_versions: dict[str, set[str]],
    full: bool,
    runtime: dict[str, dict],
) -> None:
    # ---- 6. 修订联动 ----
    l0 = asyncio.get_event_loop().time()
    linkage_enabled = runtime["node_update_linkage"]["enabled"]
    stale = (
        await asyncio.wait_for(
            check_country_report_updates(session, list(cited_versions)),
            timeout=runtime["node_update_linkage"]["timeout"],
        )
        if linkage_enabled
        else []
    )
    impacts = [
        {
            "dimension_code": dim,
            "clause_ref": s["document_id"],
            "amendment_summary": (
                f"所引用文件已出现新版本(引用 v{s['cited_version']},最新 v{s['latest_version']})"
            ),
            "action_required": "请在 72 小时内复核该维度引用条款是否受修订影响",
            "detected_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        }
        for s in stale
        for dim in sorted(cited_versions.get(s["version_id"], []))
    ]
    rv.stale_impacts_json = impacts
    link = ToolCall(
        call_id="link-1",
        tool_name="check_country_report_updates",
        tool_label="法规版本修订影响追踪",
        arguments={"cited_versions": len(cited_versions)},
        result_summary=(
            f"发现 {len(impacts)} 项受修订影响的引用" if impacts else "引用文件暂无更新版本"
        ),
        duration_ms=int((asyncio.get_event_loop().time() - l0) * 1000),
    )
    await _add_trace(
        session,
        task,
        node="node_update_linkage",
        name="法规修订 72h 影响联动 Agent",
        phase="update_linkage",
        status="passed" if linkage_enabled else "skipped",
        summary=link.result_summary
        if linkage_enabled
        else "本次运行配置关闭了报告生成时的更新检查",
        calls=[link] if linkage_enabled else [],
        duration_ms=link.duration_ms,
        inp={"cited_versions_count": len(cited_versions)},
        out={
            "cited_versions_checked": len(cited_versions) if linkage_enabled else 0,
            "stale_impacts_count": len(impacts),
            "stale_impacts": impacts,
        },
    )
    task.status = (
        "failed" if rv.status == "analysis_failed" else "completed" if full else "partial_sample"
    )
    task.updated_at = datetime.now(UTC)
    await session.commit()
