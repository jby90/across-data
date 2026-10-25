"""多智能体合规分析流水线:采集 → 检索 → 分析 → 核验(可退回补证)→ 编制 → 修订联动。

每一步的耗时、Token、工具调用均为真实运行记录;每步完成即落库,前端据此实时展示进度。
没有依据或模型不可用时,如实输出「待核查」,不编造法律结论。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
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
    CoveredClaim,
    GroundedClaim,
    SourceBoundaryError,
    SourceSupport,
    assemble_covered_claim,
    coverage_units,
    provision_boundaries,
    select_legal_quote,
    sentence_catalog,
)
from app.analysis.llm import LlmResult, LlmUnavailableError, call_llm_json
from app.analysis.models import AgentWorkflowConfig, AnalysisStepTrace, AnalysisTask, ReportVersion
from app.analysis.retrieval import task_query
from app.analysis.source_alternatives import source_notice
from app.analysis.tools import (
    ClauseCandidate,
    ToolBox,
    ToolCall,
    check_country_report_updates,
    verify_finding,
)
from app.analysis.user_facts import MAX_FACT_ROUNDS, ask_next, candidate_binding, history_event
from app.analysis.workflow_engine import (
    EIGHT_DIMENSIONS_SPEC,
    evaluate_fact_sufficiency,
)
from app.analysis.workflow_runtime import execution_settings, pin_active_version, require_executable
from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import DocumentVersion

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
    commit: bool = True,
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
    if commit:
        await session.commit()
    else:
        await session.flush()
    return tr


def _candidate_payload(c: ClauseCandidate, retained=None, fact_keys=None) -> dict:
    markers = provision_boundaries(c.text, c.heading)
    sentences = []
    context = []
    for s in sentence_catalog(c.text):
        s = {**s, "scope_id": sum(p <= s["start"] for p in markers)}
        covered_keys = {
            f["fact_key"]
            for f in (retained or [])
            if f["clause_id"] == c.clause_id
            and f["quote_start"] <= s["start"]
            and f["quote_end"] >= s["end"]
        }
        if fact_keys and set(fact_keys) <= covered_keys:
            context.append(s)
        else:
            sentences.append(s)
    return {
        "clause_id": c.clause_id,
        "document": c.document_title,
        "clause_number": c.label,
        "source_url": c.source_url,
        "version_id": c.version_id,
        "version_no": c.version_no,
        "source_nature": c.source_nature,
        "legal_status": c.legal_status,
        "instrument_identity_for_comparison_only": c.instrument_identity or None,
        "version_contract": "不同来源/版本保持独立，不得合并引文；未知时效不能认定现行有效。",
        "sentences": sentences,
        "retained_context": context if sentences else [],
    }


def _build_user_message(
    task: AnalysisTask,
    dim_code: str,
    dim_title: str,
    cands: list[ClauseCandidate],
    feedback: list[str] | None,
    retained=None,
    repair_limit=None,
    repair_keys=None,
) -> str:
    schema = AnalysisResponse.model_json_schema()
    properties = schema["$defs"]["FindingSelection"]["properties"]
    facts = {
        k: v for k, v in (task.facts_json or {}).items() if not repair_keys or k in repair_keys
    }
    candidates = [
        p for c in cands if (p := _candidate_payload(c, retained, facts)).get("sentences")
    ]
    properties["fact_key"]["enum"] = list(facts)
    properties["clause_id"]["enum"] = [c["clause_id"] for c in candidates]
    if repair_limit is not None:
        schema["properties"]["findings"]["maxItems"] = repair_limit
    payload: dict[str, Any] = {
        "phase": "select_evidence",
        "country": task.country_code,
        "vehicle_model": task.vehicle_model,
        "scenario": task.scenario_type,
        "business_facts": facts,
        "question": task_query(task.facts_json or {}, dim_code),
        "dimension": {"code": dim_code, "title": dim_title},
        "candidate_clauses": candidates,
        "selection_contract": (
            "每项只选sentences中同一scope_id的连续编号；retained_context只是上下文，不可再次选择。"
        ),
        "output_schema": schema,
        "user_fact_contract": {
            "rounds_used": getattr(task, "fact_followup_rounds", 0) or 0,
            "exhausted": (getattr(task, "fact_followup_rounds", 0) or 0) >= MAX_FACT_ROUNDS,
            "rule": "仅追问用户实际业务事实；法源、条号、版本有效性缺失必须记为证据缺口。",
        },
    }
    if retained:
        payload["retained_evidence"] = [
            {
                "clause_id": f["clause_id"],
                "fact_key": f["fact_key"],
                "sentence_ids": f.get("selected_sentence_ids") or f["sentence_ids"],
            }
            for f in retained
        ]
        payload["repair_contract"] = (
            "这些证据与结论由系统原样保留。只修复失败项，不要重新选择相同证据，也不要重写保留项。"
        )
    if feedback:
        payload["verifier_feedback_must_fix"] = feedback
    return json.dumps(payload, ensure_ascii=False)


_GROUNDING_GATE = asyncio.Semaphore(6)


async def _review_source_support(quote, obligation, config):
    """Retry only rejected JSON format; a valid no/partial verdict is final."""
    payload = {
        "phase": "verify_source_support",
        "quote": quote,
        "conclusion": obligation,
        "contract": "只核验原文是否完整支持结论；不作法律或人工审批；严格输出JSON Schema。",
        "output_schema": SourceSupport.model_json_schema(),
    }
    tokens = requests = 0
    failures = []
    ledger = []
    for attempt in range(2):
        entry = {"attempt": attempt + 1}
        ledger.append(entry)
        try:
            async with _GROUNDING_GATE:
                response = await call_llm_json(
                    [
                        {"role": "system", "content": config["system_prompt"]},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    model=config["model"],
                    temperature=config["temperature"],
                    timeout=config["timeout"],
                    max_tokens=6000,
                )
            tokens += response.total_tokens
            requests += response.request_attempts
            failures.extend(response.failures)
            entry.update(
                {
                    "attempt": attempt + 1,
                    "output": response.data,
                    "tokens": response.total_tokens,
                    "requests": response.request_attempts,
                    "failures": response.failures,
                }
            )
            grade = SourceSupport.model_validate(response.data)
            return grade, tokens, requests, failures, ledger
        except ValueError as exc:
            detail = str(
                exc.errors(include_input=False, include_url=False)
                if hasattr(exc, "errors")
                else exc
            )[:1500]
            entry["error"] = detail
            failures.append({"kind": "source_support_schema_error", "reason": detail})
            payload["schema_feedback"] = detail
        except LlmUnavailableError as exc:
            entry.update(
                error=str(exc),
                tokens=exc.tokens,
                requests=len(exc.failures),
                failures=copy.deepcopy(exc.failures),
            )
            exc.tokens += tokens
            exc.failures = failures + exc.failures
            exc.support_calls = ledger
            raise
    exc = LlmUnavailableError(
        "语义质检JSON格式连续失败", kind="model_format_error", tokens=tokens, failures=failures
    )
    exc.support_calls = ledger
    raise exc


async def _ground_selection(
    task, dim_code, dim_title, finding, candidates, config, *, support_cache=None
):
    """One isolated evidence unit per generation. Other clauses/draft rules are not sent."""
    result = dict(finding)
    # A selection-stage draft is not a legal conclusion.
    result["obligation"] = ""
    result["quote_zh"] = ""
    candidate = next((c for c in candidates if c.clause_id == finding["clause_id"]), None)
    if candidate is None:
        return result, {"requests": 0, "tokens": 0, "error": "clause_id不在检索结果中"}
    try:
        result.update(
            select_legal_quote(candidate.text, finding["sentence_ids"], heading=candidate.heading)
        )
    except SourceBoundaryError as exc:
        result["_selection_error"] = str(exc)
        return result, {"requests": 0, "tokens": 0, "error": str(exc)}
    except ValueError as exc:
        return result, {"requests": 0, "tokens": 0, "error": str(exc)}
    units = coverage_units(result["quote"])
    covered = len(units) > 1
    claim_schema = CoveredClaim if covered else GroundedClaim
    message = {
        "phase": "write_from_evidence",
        "dimension": {"code": dim_code, "title": dim_title},
        "business_fact": str(task.facts_json[finding["fact_key"]]),
        "question": task_query(task.facts_json or {}, dim_code),
        "evidence": {"quote": result["quote"]},
        "output_schema": claim_schema.model_json_schema(),
        "fidelity_checklist": [
            "主体、代词和所有格必须相同，不能新增另一主体",
            "规范性动词、行为对象、范围和全部例外保持一致",
            "条文引用保留原文层级标记，不猜译法律层级",
            "申报/许可/授权等术语有歧义时保留原词，不能自行认定性质",
            "完整条件来自原文，核验进度或缺其他材料不是本条法律条件",
        ],
    }
    if covered:
        # The source exists once in the request; full quote/hash/offset remain in
        # the server evidence record. No evidence is shortened or reconstructed by LLM.
        message["evidence"] = {"format": "source_units"}
        message["source_units"] = units
        message["coverage_contract"] = (
            "逐段完整转述source_units，每项unit_id恰好一次、按原序。不得摘要或只选相关列表项；"
            "即使与问题关系较弱，例外、替代项、修饰语、指代关系和数值范围也不能遗漏。"
            "各段共同组成同一份依据，主语可承接前段，不擅自概括为某条所有规则。"
            "任一关键行为、限定或范围因OCR残缺而不确定，则整体units=[]并填具体gap，不能补字。"
            "不要添加原文未载的币种、主体、条号、章条层级或法律性质。"
        )
    # The model sees the same exclusive rule/gap contract that Pydantic enforces.
    value_key = "units" if covered else "obligation"
    message["output_schema"]["oneOf"] = [
        {
            "required": [value_key, "gap"],
            "properties": {
                value_key: {"minItems": 1} if covered else {"minLength": 1},
                "gap": {"const": ""},
            },
        },
        {
            "required": [value_key, "gap"],
            "properties": {value_key: {"const": [] if covered else ""}, "gap": {"minLength": 1}},
        },
    ]
    tokens = requests = 0
    failures, proposals, prior_rejections, support_calls = [], [], [], []
    support_by_claim = support_cache if support_cache is not None else {}
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
                    max_tokens=6000,
                )
            tokens += response.total_tokens
            requests += response.request_attempts
            failures.extend(response.failures)
            proposals.append(
                {"attempt": attempt + 1, "output": response.data, "tokens": response.total_tokens}
            )
            claim = claim_schema.model_validate(response.data)
            obligation = assemble_covered_claim(claim, units) if covered else claim.obligation
            result.update(obligation=obligation, risk_level=claim.risk_level)
            if covered:
                result["source_coverage"] = {
                    "units": units,
                    "rendering": claim.model_dump(),
                    "human_review": None,
                }
            if claim.gap:
                result["_generation_gap"] = claim.gap
            else:
                ok, issues = verify_finding(result, candidate.text)
                if not ok:
                    raise ValueError("；".join(issues))
                if config.get("verify_source_support", False):
                    support_key = (
                        result["quote"],
                        obligation,
                        config["model"],
                        config["temperature"],
                        config["system_prompt"],
                    )
                    if support_key in support_by_claim:
                        grade = support_by_claim[support_key]
                        support_calls.append(
                            {
                                "cache_hit": True,
                                "tokens": 0,
                                "requests": 0,
                                "output": grade.model_dump(),
                            }
                        )
                    else:
                        (
                            grade,
                            review_tokens,
                            review_requests,
                            review_failures,
                            review_ledger,
                        ) = await _review_source_support(result["quote"], obligation, config)
                        tokens += review_tokens
                        requests += review_requests
                        failures.extend(review_failures)
                        support_calls.extend(review_ledger)
                        support_by_claim[support_key] = grade
                    result["source_support"] = {**grade.model_dump(), "human_review": None}
                    if grade.supported != "yes":
                        failures.append({"kind": "source_support_rejected", "reason": grade.reason})
                        if attempt == 0:
                            prior_rejections.append(copy.deepcopy(result))
                            message["semantic_feedback"] = grade.reason
                            continue
                        result["_source_support_rejected"] = grade.reason
                        break
            result["_rejected_support"] = prior_rejections
            return result, {
                "requests": requests,
                "tokens": tokens,
                "failures": failures,
                "gap": claim.gap,
                "proposals": proposals,
                "source_support_calls": support_calls,
            }
        except LlmUnavailableError as exc:
            tokens += exc.tokens
            requests += len(exc.failures)
            failures.extend(exc.failures)
            support_calls.extend(getattr(exc, "support_calls", []))
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
    result["_rejected_support"] = prior_rejections
    return result, {
        "requests": requests,
        "tokens": tokens,
        "failures": failures,
        "error": result.get("_generation_error") or result.get("_source_support_rejected"),
        "proposals": proposals,
        "source_support_calls": support_calls,
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
    _support_cache=None,
    _retained=None,
    _repair_limit=None,
    _repair_keys=None,
) -> tuple[dict[str, Any], ToolCall]:
    """单个维度的分析调用;模型不可用时返回带 error 的结果而不是编造内容。"""
    if _support_cache is None:
        _support_cache = {}
    retained = copy.deepcopy(_retained or [])
    by_id = {c.clause_id: c for c in cands}
    for f in retained:
        c = by_id.get(f["clause_id"])
        bound = (
            select_legal_quote(
                c.text, f.get("selected_sentence_ids") or f["sentence_ids"], heading=c.heading
            )
            if c
            else {}
        )
        if (
            c is None
            or f.get("source_text_sha256") != hashlib.sha256(c.text.encode()).hexdigest()
            or c.text[f["quote_start"] : f["quote_end"]] != f["quote"]
            or not verify_finding(f, c.text)[0]
            or bound["quote_start"] != f["quote_start"]
            or bound["quote_end"] != f["quote_end"]
            or (
                config.get("verify_source_support")
                and f.get("source_support", {}).get("supported") != "yes"
            )
        ):
            raise ValueError("保留证据已失效或未通过原核验，禁止复用")
    msgs = [
        {"role": "system", "content": config["system_prompt"]},
        {
            "role": "user",
            "content": _build_user_message(
                task, dim_code, dim_title, cands, feedback, retained, _repair_limit, _repair_keys
            ),
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
                    available = {
                        p["clause_id"]: p
                        for p in json.loads(msgs[1]["content"])["candidate_clauses"]
                    }
                    for f in validated.findings:
                        if _repair_keys and f.fact_key not in _repair_keys:
                            raise ValueError("仅允许修复失败事实键")
                        if retained:
                            p = available.get(f.clause_id)
                            allowed = (
                                {s["sentence_id"]: s["scope_id"] for s in p["sentences"]}
                                if p
                                else {}
                            )
                            if any(i not in allowed for i in f.sentence_ids):
                                raise ValueError("所选证据不在本轮可选范围，不能重复保留项")
                    if _repair_limit is not None and len(validated.findings) > _repair_limit:
                        raise ValueError("只重试失败项，不得超过本次修复名额")
                    for f in validated.findings if retained else []:
                        c = by_id.get(f.clause_id)
                        if c is None:
                            continue
                        bound = select_legal_quote(c.text, f.sentence_ids, heading=c.heading)
                        if any(
                            old["clause_id"] == f.clause_id
                            and old["fact_key"] == f.fact_key
                            and old["quote_start"] == bound["quote_start"]
                            and old["quote_end"] == bound["quote_end"]
                            for old in retained
                        ):
                            raise ValueError("保留项被重复选择，请只修复失败项")
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
                        retained,
                        _repair_limit,
                        _repair_keys,
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
                    support_cache=_support_cache,
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

            grounded = (
                []
                if validated.needs_user_fact
                else await asyncio.gather(*[ground_one(f) for f in validated.findings])
            )
            refused = [
                f
                for f in grounded
                if f.get("_generation_gap")
                or f.get("_generation_error")
                or f.get("_source_support_rejected")
                or f.get("_selection_error")
            ]
            if refused and attempts < _attempt_budget:
                _verify_dimension(dim_code, {"findings": grounded}, cands)
                kept = [f for f in grounded if f.get("_ok")]
                rejected = [f for f in grounded if not f.get("_ok")]
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
                        "上次依据生成未通过，请按具体原因重新选择完整依据；不能补字或放宽核验。",
                        *[
                            f["clause_id"]
                            + ": "
                            + (
                                f.get("_generation_gap")
                                or f.get("_generation_error")
                                or f.get("_source_support_rejected")
                                or f["_selection_error"]
                            )
                            for f in refused
                        ],
                    ],
                    config,
                    _attempt_budget=_attempt_budget - attempts,
                    _support_cache=_support_cache,
                    _retained=retained + kept,
                    _repair_limit=len(rejected),
                    _repair_keys={f["fact_key"] for f in rejected},
                )
                revised["tokens"] += used_tokens
                call.arguments.update(
                    retry=bool(feedback),
                    schema_attempts=attempts + call.arguments["schema_attempts"],
                    request_attempts=request_attempts + call.arguments["request_attempts"],
                    failures=failures + call.arguments.get("failures", []),
                    grounding_calls=grounding_ledger + call.arguments["grounding_calls"],
                    selection_retries=[
                        {
                            "reason": "source_or_generation_validation",
                            "findings": rejected,
                            "retained_findings": kept,
                        }
                    ],
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
            "findings": retained,
        }, call
    data = validated.model_dump()
    data["evidence_selections"] = [
        {
            "clause_id": f["clause_id"],
            "sentence_ids": f.get("selected_sentence_ids") or f["sentence_ids"],
            "fact_key": f["fact_key"],
            "retained": True,
        }
        for f in retained
    ] + [
        {"clause_id": f.clause_id, "sentence_ids": f.sentence_ids, "fact_key": f.fact_key}
        for f in validated.findings
    ]
    data["findings"] = retained + grounded
    data["repair_unresolved"] = bool(retained and _repair_limit and not grounded)
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
            "retained_count": len(retained),
            "repair_limit": _repair_limit,
            "model": config["model"],
            "temperature": config["temperature"],
            "timeout": config["timeout"],
        },
        result_summary=(
            f"依据生成 {len(grounded)} 项；保留已有核验结果 {len(retained)} 项；"
            f"已知Token {used_tokens}"
        ),
        duration_ms=int((time.perf_counter() - started) * 1000),
        verified=not data["repair_unresolved"]
        and not any(entry.get("error") or entry.get("gap") for entry in grounding_ledger),
    )
    return data, call


def _verify_dimension(
    dim_code: str, result: dict[str, Any], cands: list[ClauseCandidate]
) -> list[str]:
    """核验一个维度的全部结论,把通过/失败写回 finding,并返回问题清单。"""
    by_id = {c.clause_id: c for c in cands}
    problems: list[str] = []
    if result.get("user_fact_gap") and not result.get("error"):
        return problems  # Exhausted business facts cannot be repaired by legal-evidence retries.
    if cands and (
        result.get("error") or result.get("repair_unresolved") or not result.get("findings")
    ):
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
                    cand.text,
                    f.get("selected_sentence_ids") or f.get("sentence_ids") or [],
                    heading=cand.heading,
                )
            )
            if f.get("_generation_error") or f.get("_generation_gap"):
                ok, issues = False, [f.get("_generation_error") or f["_generation_gap"]]
            else:
                ok, issues = verify_finding(f, cand.text)
            f["_program_ok"] = ok
            if ok and f.get("source_support", {}).get("supported", "yes") != "yes":
                ok, issues = False, ["结论语义核验未通过：" + f["source_support"]["reason"]]
        except ValueError as exc:
            ok, issues = False, [str(exc)]
            f["_program_ok"] = False
        f["_ok"] = ok
        f["_issues"] = issues
        if not ok:
            problems.append(
                f"{dim_code} {cand.document_title[:30]} {cand.label}: " + ";".join(issues)
            )
    return problems


async def _pause_for_facts(session, task, results, dim_cands):
    from app.analysis.chat import add_message
    from app.analysis.evidence_selection import UserFactNeed

    requests = {}
    for code, result in results.items():
        for raw in result.get("needs_user_fact", []):
            need = UserFactNeed.model_validate(raw).model_dump()
            key = need["fact_key"]
            if key not in requests:
                requests[key] = need | {"id": str(uuid.uuid4()), "dimension_codes": []}
            requests[key]["dimension_codes"].append(code)
            if need["question_zh"] not in requests[key]["question_zh"]:
                requests[key]["question_zh"] += "\n" + need["question_zh"]
                requests[key]["why_needed"] += "\n" + need["why_needed"]
    if not requests:
        return False
    if task.fact_followup_rounds >= MAX_FACT_ROUNDS:
        for result in results.values():
            if result.get("needs_user_fact"):
                result["user_fact_gap"] = copy.deepcopy(result["needs_user_fact"])
                result["findings"] = []
                result["gaps"] = "业务事实经两轮追问仍不足：" + "；".join(
                    item["question_zh"] for item in result["needs_user_fact"]
                )
                result["needs_user_fact"] = []
        history_event(task, "exhausted", requests=list(requests.values()))
        await add_message(
            session,
            task.id,
            "assistant",
            "两轮追问后仍有业务事实无法确认，将在对应报告维度保留事实缺口，不推断答案。",
        )
        await _add_trace(
            session,
            task,
            node="node_fact_collector",
            name="业务事实追问达到上限",
            phase="user_fact_exhausted",
            status="warning",
            calls=[],
            summary="最多两轮追问已完成；事实不足的维度保留缺口",
            out={"requests": list(requests.values())},
        )
        return False
    task.fact_followup_rounds += 1
    task.pending_user_facts_json = list(requests.values())
    # Recheck retained proposals now; the checkpoint is not an automatic approval.
    for code, result in results.items():
        if not result.get("needs_user_fact"):
            _verify_dimension(code, result, dim_cands.get(code, []))
    checkpoint = {
        "workflow_version_id": str(task.workflow_version_id),
        "country_code": task.country_code,
        "vehicle_model": task.vehicle_model,
        "scenario_type": task.scenario_type,
        "facts": copy.deepcopy(task.facts_json),
        "results": copy.deepcopy(results),
        "candidate_bindings": {
            code: [candidate_binding(c) for c in values] for code, values in dim_cands.items()
        },
        "resume_dimensions": [],
    }
    task.analysis_checkpoint_json = checkpoint
    task.status = "awaiting_user_facts"
    history_event(task, "asked", requests=task.pending_user_facts_json)
    await ask_next(session, task)
    await _add_trace(
        session,
        task,
        node="node_fact_collector",
        name="分析中业务事实追问",
        phase="user_fact_question",
        status="awaiting_user_facts",
        calls=[],
        commit=False,
        summary=f"分析暂停，等待 {len(requests)} 项业务事实（第 {task.fact_followup_rounds}/2 轮）",
        out={
            "requests": task.pending_user_facts_json,
            "checkpoint": checkpoint,
            "retained_dimensions": [c for c, r in results.items() if not r.get("needs_user_fact")],
        },
    )
    await session.commit()
    return True


async def run_pipeline(session: AsyncSession, task_id: uuid.UUID) -> None:
    task = await session.get(AnalysisTask, task_id)
    if task is None:
        return
    if task.status == "awaiting_user_facts":
        return  # Manual rerun cannot bypass unanswered questions.
    checkpoint = copy.deepcopy(task.analysis_checkpoint_json or {})
    resume = bool(checkpoint.get("resume_dimensions"))
    cc = task.country_code.upper()
    cfg = (
        await session.get(AgentWorkflowConfig, task.workflow_version_id)
        if task.workflow_version_id
        else None
    )
    if cfg is None:
        cfg = await pin_active_version(session, task)
    require_executable(cfg)
    if resume and checkpoint.get("workflow_version_id") != str(cfg.id):
        raise ValueError("断点与工作流版本不一致，禁止复用结果")
    if resume and any(
        checkpoint.get(k) != getattr(task, k)
        for k in ("country_code", "vehicle_model", "scenario_type")
    ):
        raise ValueError("断点业务场景已变化，禁止复用其他场景结果")
    runtime = {n["node_id"]: execution_settings(n) for n in cfg.nodes_json}
    task.status = "running"
    if not resume:
        task.total_tokens = task.total_duration_ms = task.mcp_calls_count = 0
        task.backtrack_count = task.fact_followup_rounds = 0
        task.pending_user_facts_json = []
        task.analysis_checkpoint_json = {}
        task.findings_json = []
    await session.commit()

    tb = ToolBox(session, cc, cache_scope=str(task.org_id or task.created_by))

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
    if pending and not resume:
        task.status = "collecting_facts"
        await session.commit()
        return

    # ---- 2. 检索 ----
    mark = len(tb.calls)
    run_codes = (
        set(checkpoint["resume_dimensions"]) if resume else dict(EIGHT_DIMENSIONS_SPEC).keys()
    )
    retained_results = copy.deepcopy(checkpoint.get("results", {})) if resume else {}
    retained_cands = {}

    async def retrieve():
        availability = await tb.check_source_availability()
        if resume:
            fresh = {c.clause_id: c for c in await tb._load_candidates()}
            changed_facts = {
                k
                for k in set(checkpoint["facts"]) | set(task.facts_json)
                if checkpoint["facts"].get(k) != task.facts_json.get(k)
            }
            for code, result in retained_results.items():
                if any(f.get("fact_key") in changed_facts for f in result.get("findings", [])):
                    run_codes.add(code)
            archive = ArchiveStore(get_settings().archive_dir)
            checked_versions = set()
            for code, bindings in checkpoint["candidate_bindings"].items():
                if code in run_codes:
                    continue
                if all(
                    b["clause_id"] in fresh and candidate_binding(fresh[b["clause_id"]]) == b
                    for b in bindings
                ):
                    for binding in bindings:
                        vid = binding["version_id"]
                        if vid not in checked_versions:
                            version = await session.get(DocumentVersion, uuid.UUID(vid))
                            raw = archive.read(version.archive_path)
                            if (
                                archive.sha256(raw) != version.content_hash
                                or len(raw) != version.size_bytes
                            ):
                                raise ValueError("断点原文归档完整性失败，禁止复用")
                            checked_versions.add(vid)
                    retained_cands[code] = [fresh[b["clause_id"]] for b in bindings]
                else:
                    run_codes.add(code)
            for code in run_codes:
                retained_results.pop(code, None)
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
                if code in run_codes
            ]
        )
        dim_cands = retained_cands | dict(
            zip((c for c, _ in EIGHT_DIMENSIONS_SPEC if c in run_codes), searched, strict=True)
        )
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
    results: dict[str, dict[str, Any]] = copy.deepcopy(retained_results)
    active = [code for code, _ in EIGHT_DIMENSIONS_SPEC if code in run_codes and dim_cands[code]]
    support_caches = {code: {} for code in active}
    t_start = asyncio.get_event_loop().time()
    outs = await asyncio.gather(
        *[
            _analyse_dimension(
                task,
                c,
                dim_titles[c],
                dim_cands[c],
                None,
                runtime["node_dimension_analyst"],
                _support_cache=support_caches[c],
            )
            for c in active
        ]
    )
    wall = int((asyncio.get_event_loop().time() - t_start) * 1000)
    llm_calls: list[ToolCall] = []
    for code, (data, call) in zip(active, outs, strict=True):
        results[code] = data
        llm_calls.append(call)
    tokens = sum(int(results[c].get("tokens") or 0) for c in active)
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
            "retained_dimensions": sorted(retained_results),
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

    if await _pause_for_facts(session, task, results, dim_cands):
        return

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
        task.backtrack_count += 1
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
                    _support_cache=support_caches[c],
                    _retained=[f for f in results[c].get("findings", []) if f.get("_ok")],
                    _repair_limit=max(
                        1, sum(not f.get("_ok") for f in results[c].get("findings", []))
                    ),
                    _repair_keys={
                        f["fact_key"] for f in results[c].get("findings", []) if not f.get("_ok")
                    },
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
        if await _pause_for_facts(session, task, results, dim_cands):
            return
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
                    "version_id": cand.version_id,
                    "source_nature": cand.source_nature,
                    "source_notice": source_notice(cand.source_nature),
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
                            "source_support",
                            "source_coverage",
                        )
                    },
                }
            )
            lines.append(f"- {tag} {f.get('obligation', '')}(风险:{risk})")
            if source_notice(cand.source_nature):
                lines.append(f"  来源说明：{source_notice(cand.source_nature)}。")
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
                    "version_id": cand.version_id,
                    "source_nature": cand.source_nature,
                    "source_notice": source_notice(cand.source_nature),
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
                            "source_support",
                            "source_coverage",
                        )
                    },
                }
            )
        gap: str | None = None
        if r.get("error"):
            gap = "分析技术失败（模型调用或格式错误），请重试；不能当作法源不足"
        elif r.get("user_fact_gap"):
            gap = r["gaps"]
        elif r.get("repair_unresolved"):
            gap = "仍有补证任务未完成；已有核验结果保留，不能标记为完整报告"
        elif not dim_cands.get(code):
            gap = "已归档法源中未检索到与该维度相关的可用条款(待核查)"
        elif not good:
            gap = r.get("gaps") or "模型未能给出可通过原文核验的结论(待核查)"
        elif dropped:
            gap = f"另有 {dropped} 条结论未通过原文核验,已剔除(待核查)"
        technical_failure = bool(
            r.get("error")
            or (
                not r.get("user_fact_gap")
                and (r.get("repair_unresolved") or dropped or (dim_cands.get(code) and not good))
            )
        )
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
                else "user_fact_gap"
                if r.get("user_fact_gap")
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
            else "尚有维度存在业务事实或可用法源缺口,已逐维标注,本报告为部分样稿。"
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
    task.analysis_checkpoint_json = {}
    task.pending_user_facts_json = []
    task.updated_at = datetime.now(UTC)
    await session.commit()
