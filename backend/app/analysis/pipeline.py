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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.llm import LlmResult, LlmUnavailableError, call_llm_json
from app.analysis.models import AnalysisStepTrace, AnalysisTask, ReportVersion
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

log = logging.getLogger(__name__)

_RISK_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
_background: set[asyncio.Task] = set()

SYSTEM_PROMPT = (
    "你是汽车数据跨境合规分析智能体。只能依据用户消息里提供的候选条款作答。\n"
    "规则:\n"
    "1. 每条 finding 必须引用候选条款中的一段原文 quote:逐字复制该条款原文的连续片段,"
    "不得改写、翻译、增删或用省略号拼接,长度 20~300 字符。\n"
    "2. clause_id 必须取自候选条款列表。\n"
    "3. obligation 用中文概括该条款对企业的合规义务;其中出现的数字、金额、期限"
    "必须与引用原文一致,原文没有的数字不得写。\n"
    "4. 候选条款不足以支撑时不要编造,findings 可为空,并在 gaps 说明缺少什么依据。\n"
    "5. 只输出 JSON 对象:"
    '{"summary":"中文小结","findings":[{"business_fact":"对应的业务事实",'
    '"clause_id":"...","obligation":"...","risk_level":"HIGH|MEDIUM|LOW",'
    '"quote":"...","quote_zh":"引用片段的中文翻译(机器翻译)"}],'
    '"recommendations":["中文整改建议"],"gaps":"缺口说明,没有则为空字符串"}'
)


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
    tr = AnalysisStepTrace(
        task_id=task.id,
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
        "text": c.text[:1500],
    }


def _build_user_message(
    task: AnalysisTask,
    dim_code: str,
    dim_title: str,
    cands: list[ClauseCandidate],
    feedback: list[str] | None,
) -> str:
    payload: dict[str, Any] = {
        "country": task.country_code,
        "vehicle_model": task.vehicle_model,
        "scenario": task.scenario_type,
        "business_facts": task.facts_json or {},
        "dimension": {"code": dim_code, "title": dim_title},
        "candidate_clauses": [_candidate_payload(c) for c in cands],
    }
    if feedback:
        payload["verifier_feedback_must_fix"] = feedback
    return json.dumps(payload, ensure_ascii=False)


async def _analyse_dimension(
    task: AnalysisTask,
    dim_code: str,
    dim_title: str,
    cands: list[ClauseCandidate],
    feedback: list[str] | None,
) -> tuple[dict[str, Any], ToolCall]:
    """单个维度的分析调用;模型不可用时返回带 error 的结果而不是编造内容。"""
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_message(task, dim_code, dim_title, cands, feedback),
        },
    ]
    try:
        res: LlmResult = await call_llm_json(msgs)
    except LlmUnavailableError as exc:
        call = ToolCall(
            call_id=f"llm-{dim_code}",
            tool_name="llm.chat",
            tool_label=f"{dim_code} 维度分析(模型调用)",
            arguments={"dimension": dim_code, "candidates": len(cands)},
            result_summary=str(exc),
            duration_ms=0,
            verified=False,
        )
        return {"error": str(exc), "findings": []}, call
    findings = res.data.get("findings")
    data = {
        "summary": str(res.data.get("summary") or ""),
        "findings": findings if isinstance(findings, list) else [],
        "recommendations": [str(x) for x in (res.data.get("recommendations") or [])][:6],
        "gaps": str(res.data.get("gaps") or ""),
        "tokens": res.total_tokens,
        "model": res.model,
    }
    call = ToolCall(
        call_id=f"llm-{dim_code}",
        tool_name="llm.chat",
        tool_label=f"{dim_code} 维度分析(模型 {res.model})",
        arguments={
            "dimension": dim_code,
            "candidates": len(cands),
            "retry": bool(feedback),
        },
        result_summary=(
            f"返回 {len(data['findings'])} 条结论;Token {res.prompt_tokens}+{res.completion_tokens}"
        ),
        duration_ms=res.duration_ms,
    )
    return data, call


def _verify_dimension(
    dim_code: str, result: dict[str, Any], cands: list[ClauseCandidate]
) -> list[str]:
    """核验一个维度的全部结论,把通过/失败写回 finding,并返回问题清单。"""
    by_id = {c.clause_id: c for c in cands}
    problems: list[str] = []
    for f in result.get("findings", []):
        if not isinstance(f, dict):
            continue
        cand = by_id.get(str(f.get("clause_id")))
        if cand is None:
            f["_ok"] = False
            f["_issues"] = ["clause_id 不在检索结果中"]
            problems.append(f"{dim_code}: 引用了检索结果之外的条款")
            continue
        ok, issues = verify_finding(f, cand.text)
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
    await session.execute(delete(AnalysisStepTrace).where(AnalysisStepTrace.task_id == task.id))
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
        out={"sufficiency_score": score, "missing": pending},
    )
    if pending:
        task.status = "collecting_facts"
        await session.commit()
        return

    # ---- 2. 检索 ----
    mark = len(tb.calls)
    availability = await tb.check_source_availability()
    dim_cands: dict[str, list[ClauseCandidate]] = {}
    for code, _ in EIGHT_DIMENSIONS_SPEC:
        dim_cands[code] = await tb.search_effective_clauses(code)
    all_cands = {c.clause_id: c for lst in dim_cands.values() for c in lst}
    await tb.query_kg_obligations(list(all_cands))
    await tb.verify_clause_effective_date(list(all_cands.values()))
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
        duration_ms=sum(c.duration_ms for c in step_calls),
        inp={"country_code": cc},
        out={"candidates_per_dimension": {k: len(v) for k, v in dim_cands.items()}},
    )

    # ---- 3. 分析(8 个维度并行调用模型) ----
    dim_titles = dict(EIGHT_DIMENSIONS_SPEC)
    results: dict[str, dict[str, Any]] = {}
    active = [code for code, _ in EIGHT_DIMENSIONS_SPEC if dim_cands[code]]
    t_start = asyncio.get_event_loop().time()
    outs = await asyncio.gather(
        *[_analyse_dimension(task, c, dim_titles[c], dim_cands[c], None) for c in active]
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
        status="failed" if errors and len(errors) == len(llm_calls) else "passed",
        summary=(
            f"对 {len(active)} 个有候选条款的维度调用模型分析,共得到 "
            f"{sum(len(r.get('findings', [])) for r in results.values())} 条待核验结论"
            + (f";{len(errors)} 个维度模型调用失败,如实标记为待核查" if errors else "")
        ),
        calls=llm_calls,
        tokens=tokens,
        duration_ms=wall,
        out={"dimensions_analysed": active},
    )

    # ---- 4. 核验(程序化子串/数字校验) ----
    async def _verify_round(codes: list[str], phase: str) -> list[str]:
        v0 = asyncio.get_event_loop().time()
        calls: list[ToolCall] = []
        problems: list[str] = []
        for code in codes:
            r = results.get(code)
            if not r or r.get("error"):
                continue
            c0 = asyncio.get_event_loop().time()
            probs = _verify_dimension(code, r, dim_cands[code])
            n = len(r.get("findings", []))
            calls.append(
                ToolCall(
                    call_id=f"verify-{phase}-{code}",
                    tool_name="verify_quote_substring",
                    tool_label=f"{code} 引用子串与数字核验",
                    arguments={"dimension": code, "findings": n},
                    result_summary=(
                        f"{n - len(probs)}/{n} 条通过" + (f";{'; '.join(probs)}" if probs else "")
                    ),
                    duration_ms=int((asyncio.get_event_loop().time() - c0) * 1000),
                    verified=not probs,
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
            status="backtracked" if problems else "passed",
            summary=(
                f"核验未通过 {len(problems)} 条,退回分析 Agent 补充依据"
                if problems
                else "全部结论的引用均为条款原文连续子串,数字与原文一致"
            ),
            calls=calls,
            duration_ms=ms,
            is_backtrack=bool(problems),
            reason="\n".join(problems) if problems else None,
            out={"problems": problems},
        )
        return problems

    problems = await _verify_round(list(results), "verification")
    if problems:
        task.backtrack_count = 1
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
            status="passed",
            summary=f"按质检退回意见对 {len(bad_codes)} 个维度重新分析并补充依据",
            calls=calls2,
            tokens=tokens2,
            duration_ms=int((asyncio.get_event_loop().time() - b0) * 1000),
            is_backtrack=True,
            reason="\n".join(problems),
        )
        problems2 = await _verify_round(bad_codes, "re_verification")
        redo.backtrack_resolution = (
            "二次核验全部通过"
            if not problems2
            else f"二次核验仍有 {len(problems2)} 条未通过,已剔除并标记为待核查"
        )
        await session.commit()

    # ---- 5. 编制 ----
    await _compile(session, task, tb, results, dim_cands, dim_titles)


async def _compile(
    session: AsyncSession,
    task: AnalysisTask,
    tb: ToolBox,
    results: dict[str, dict[str, Any]],
    dim_cands: dict[str, list[ClauseCandidate]],
    dim_titles: dict[str, str],
) -> None:
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
                }
            )
        gap: str | None = None
        if r.get("error"):
            gap = "模型调用失败,未生成该维度分析,结论待核查,请稍后重新分析(技术细节见协同过程)"
        elif not dim_cands.get(code):
            gap = "已归档法源中未检索到与该维度相关的可用条款(待核查)"
        elif not good:
            gap = r.get("gaps") or "模型未能给出可通过原文核验的结论(待核查)"
        elif dropped:
            gap = f"另有 {dropped} 条结论未通过原文核验,已剔除(待核查)"
        body = ""
        if good and not dropped and r.get("summary"):
            body += r["summary"] + "\n\n"
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
                "status": "COMPLETE" if good and not gap else "PARTIAL_GAP",
                "risk_level": max(levels, key=lambda x: _RISK_ORDER[x])
                if levels
                else "PENDING_VERIFICATION",
                "content_markdown": body,
                "key_recommendations": (r.get("recommendations") or [])
                if good and not dropped
                else [],
                "citations": citations,
                "missing_gap_note": gap,
            }
        )
    covered = sum(1 for d in dimensions if d["citations"])
    full = all(d["status"] == "COMPLETE" for d in dimensions)
    summary = (
        f"本报告针对【{task.vehicle_model}】在【{task.country_code}】的业务场景,"
        f"由多智能体检索条款、模型分析并经程序核验:共 {len(findings)} 条结论通过原文核验,"
        f"覆盖 {covered}/8 个维度。所有引用均为官方原文,中文为机器翻译,尚未经律所复核。"
        + ("" if full else "尚有维度缺少可核验依据,已逐维标注缺口,本报告为部分样稿。")
    )
    rvs = list(
        (await session.execute(select(ReportVersion).where(ReportVersion.task_id == task.id)))
        .scalars()
        .all()
    )
    rv = ReportVersion(
        task_id=task.id,
        version_no=max((r.version_no for r in rvs), default=0) + 1,
        status="full_draft" if full else "partial_sample",
        is_partial_sample=not full,
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
        status="passed" if full else "warning",
        summary=(
            f"编制完成:{len(findings)} 条核验通过的结论,{covered}/8 个维度有依据"
            + ("" if full else ";缺依据的维度已标注缺口,报告为部分样稿")
        ),
        calls=[comp],
        duration_ms=comp.duration_ms,
        out={"report_version": rv.version_no},
    )

    # ---- 6. 修订联动 ----
    l0 = asyncio.get_event_loop().time()
    stale = await check_country_report_updates(session, list(cited_versions))
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
        status="passed",
        summary=link.result_summary,
        calls=[link],
        duration_ms=link.duration_ms,
    )
    task.status = "completed" if full else "partial_sample"
    task.updated_at = datetime.now(UTC)
    await session.commit()
