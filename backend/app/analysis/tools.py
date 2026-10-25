"""多智能体使用的内置工具(MCP 风格:有名称、入参、出参,每次调用都计时并留痕)。

所有工具都直接读取 M1 法规知识库,不使用任何写死的条款。
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.retrieval import RETRIEVAL_VERSION, ClauseIndex
from app.ingestion.layout_safety import layout_held_clause_ids
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_available
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.models import ClauseTranslation, KgEdge, KgNode
from app.knowledge.numeric_integrity import amounts, dates, tokens

# 每个维度的多语种检索关键词(英/法/阿)。仅用于检索候选条款,不代表结论。
DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "D1": [
        "personal data",
        "data protection",
        "protection des données",
        "caractère personnel",
        "البيانات الشخصية",
        "حماية البيانات",
        "scope",
        "application",
        "application of act",
        "champ d’application",
        "s’applique",
        "تطبق",
    ],
    "D2": [
        "personal data",
        "means",
        "definition",
        "identif",
        "location",
        "biometric",
        "on entend par",
        "définition",
        "يقصد",
        "تعريف",
    ],
    "D3": [
        "transfer",
        "abroad",
        "overseas",
        "cross-border",
        "transfert",
        "étranger",
        "autorisation",
        "نقل",
        "خارج",
    ],
    "D4": [
        "consent",
        "notif",
        "purpose",
        "data protection officer",
        "consentement",
        "information",
        "موافقة",
        "إخطار",
    ],
    "D5": [
        "retention",
        "retain",
        "cease to retain",
        "destroy",
        "delete",
        "conservation",
        "durée",
        "الاحتفاظ",
        "حفظ",
    ],
    "D6": [
        "penalty",
        "fine",
        "financial penalty",
        "imprison",
        "offence",
        "turnover",
        "amende",
        "emprisonnement",
        "sanction",
        "غرامة",
        "عقوبة",
    ],
    "D7": [
        "authoris",
        "approval",
        "licen",
        "trial",
        "autonomous",
        "registration",
        "permit",
        "déclaration",
        "ترخيص",
    ],
    "D8": [
        "compliance",
        "accountab",
        "security",
        "safeguard",
        "breach",
        "obligation",
        "mesures",
        "conformité",
        "امتثال",
        "أمن",
    ],
}

_TOC_DOTS = re.compile(r"\.{5,}")
_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f\u0600-\u06ff\u4e00-\u9fff]")
MIN_CLAUSE_CHARS = 80


@dataclass
class ClauseCandidate:
    clause_id: str
    document_id: str
    document_title: str
    version_id: str
    version_no: int
    label: str
    text: str
    source_url: str
    review_status: str
    legal_status: str
    translated_text: str = ""
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    heading: str = ""
    language: str = ""


@dataclass
class ToolCall:
    """一次工具调用的真实记录。"""

    call_id: str
    tool_name: str
    tool_label: str
    arguments: dict[str, Any]
    result_summary: str
    duration_ms: int
    verified: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "tool_label": self.tool_label,
            "arguments": self.arguments,
            "result_summary": self.result_summary,
            "duration_ms": self.duration_ms,
            "verified": self.verified,
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_usable_clause_text(text: str) -> bool:
    """过滤目录页、过短片段和乱码(字母占比过低)。"""
    t = _norm(text)
    if len(t) < MIN_CLAUSE_CHARS:
        return False
    if len(_TOC_DOTS.findall(t)) >= 2:
        return False
    letters = len(_LETTER.findall(t))
    return letters / max(len(t), 1) >= 0.5


def clause_label(number: str | None, ordinal: int, page: int | None) -> str:
    if number:
        return number
    if page:
        return f"第{page}页"
    return f"第{ordinal + 1}段"


def clause_relevance(candidate: ClauseCandidate, keywords: list[str]) -> tuple[float, list[str]]:
    """Rank actual clause content/heading; a repeated document title is not body evidence."""
    body = _norm(candidate.text).casefold()
    # Parsed heading preferred; leading source text handles numbered FR/AR clauses.
    first_line = candidate.text.splitlines()[0] if candidate.text.splitlines() else ""
    heading = _norm(candidate.heading or (first_line if len(first_line) <= 120 else ""))
    heading = heading.casefold()[:220]
    matched = [k for k in keywords if k in body]
    if not matched:
        return 0, []
    heading_hits = sum(k in heading for k in keywords)
    title_hits = sum(k in candidate.document_title.casefold() for k in keywords)
    score = sum(min(body.count(k), 3) for k in matched) + 2 * len(matched)
    return score + 8 * heading_hits + 0.15 * title_hits, matched


class ToolBox:
    """一次分析任务的工具箱;记录每次调用供「任务过程」展示。"""

    def __init__(self, session: AsyncSession, country_code: str):
        self.session = session
        self.country_code = country_code.upper()
        self._seq = 0
        self.calls: list[ToolCall] = []
        self._candidates_cache: list[ClauseCandidate] | None = None
        self._index: ClauseIndex | None = None
        self.retrieval_tokens = 0

    def _record(
        self,
        name: str,
        label: str,
        args: dict[str, Any],
        summary: str,
        started: float,
        verified: bool = True,
    ) -> ToolCall:
        self._seq += 1
        call = ToolCall(
            call_id=f"call-{self._seq:02d}",
            tool_name=name,
            tool_label=label,
            arguments=args,
            result_summary=summary,
            duration_ms=int((time.perf_counter() - started) * 1000),
            verified=verified,
        )
        self.calls.append(call)
        return call

    async def _load_candidates(self) -> list[ClauseCandidate]:
        """加载该国每份文件最新已解析版本的全部条款。"""
        if self._candidates_cache is not None:
            return self._candidates_cache
        rows = list(
            (
                await self.session.execute(
                    select(Clause, DocumentVersion, Document)
                    .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(Document.country_code == self.country_code)
                    .where(document_available())
                    .where(Document.relevance.notin_(("irrelevant", "out_of_scope")))
                    .where(DocumentVersion.state == "parsed")
                )
            ).all()
        )
        latest: dict[uuid.UUID, int] = {}
        for _, ver, doc in rows:
            latest[doc.id] = max(latest.get(doc.id, 0), ver.version_no)
        clause_ids = [c.id for c, ver, doc in rows if ver.version_no == latest[doc.id]]
        layout_held = set(await layout_held_clause_ids(self.session, clause_ids))
        trs = (
            list(
                (
                    await self.session.execute(
                        select(ClauseTranslation).where(
                            ClauseTranslation.clause_id.in_(clause_ids),
                            ClauseTranslation.is_current.is_(True),
                            ClauseTranslation.review_status.notin_(
                                (
                                    "superseded",
                                    "not_translated",
                                    "translation_failed",
                                    "rejected",
                                    "stale",
                                )
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if clause_ids
            else []
        )
        tr_map = {t.clause_id: t for t in trs}
        out: list[ClauseCandidate] = []
        for clause, ver, doc in rows:
            if (
                ver.version_no != latest[doc.id]
                or clause.id in layout_held
                or ver.legal_status == "repealed"
            ):
                continue
            tr = tr_map.get(clause.id)
            out.append(
                ClauseCandidate(
                    clause_id=str(clause.id),
                    document_id=str(doc.id),
                    document_title=doc.title,
                    version_id=str(ver.id),
                    version_no=ver.version_no,
                    label=clause_label(clause.clause_number, clause.ordinal, clause.page_start),
                    text=clause.original_text,
                    source_url=doc.source_url,
                    review_status=tr.review_status if tr else "pending_review",
                    legal_status=ver.legal_status,
                    heading=clause.heading or "",
                    language=clause.language or "",
                    translated_text=tr.translated_text if tr else "",
                )
            )
        self._candidates_cache = out
        return out

    async def check_source_availability(self) -> dict[str, Any]:
        started = time.perf_counter()
        cands = await self._load_candidates()
        usable = [c for c in cands if is_usable_clause_text(c.text)]
        docs = {c.document_id for c in cands}
        statuses: dict[str, int] = {}
        for c in cands:
            statuses[c.legal_status] = statuses.get(c.legal_status, 0) + 1
        result = {
            "documents": len(docs),
            "clauses_total": len(cands),
            "clauses_usable": len(usable),
            "legal_status_counts": statuses,
        }
        self._record(
            "check_source_availability",
            "国别法源可用性前置检查",
            {"country_code": self.country_code},
            f"{len(docs)} 份文件、{len(cands)} 条条款,其中可用于检索 {len(usable)} 条"
            f"(已过滤目录页/过短/乱码);现行状态分布 {statuses}",
            started,
            verified=len(usable) > 0,
        )
        return result

    async def search_effective_clauses(
        self,
        dimension: str,
        keywords: list[str] | None = None,
        top_k: int = 5,
        query: str | None = None,
        config: dict | None = None,
    ) -> list[ClauseCandidate]:
        started = time.perf_counter()
        kws = [k.lower() for k in (keywords or DIMENSION_KEYWORDS.get(dimension, []))]
        if self._index is None:
            candidates = [c for c in await self._load_candidates() if is_usable_clause_text(c.text)]
            self._index = ClauseIndex(candidates)
        from app.analysis.workflow_engine import EIGHT_DIMENSIONS_SPEC

        dimension_title = dict(EIGHT_DIMENSIONS_SPEC).get(dimension, "")
        scored = self._index.rank(" ".join(kws) + " " + dimension_title, query)
        planning = {}
        if config and config.get("semantic_planning") and self._index.candidates:
            from app.analysis.retrieval_planner import planned_search

            scored, planning = await planned_search(
                self._index,
                " ".join(kws) + " " + dimension_title,
                dimension_title,
                query,
                top_k,
                config,
            )
            self.retrieval_tokens += planning["tokens"]
        top = [
            ClauseCandidate(
                **{
                    **c.__dict__,
                    "score": score,
                    "matched_keywords": [k for k in kws if k in c.text.lower()],
                }
            )
            for c, score in scored[:top_k]
        ]
        self._record(
            "search_effective_clauses",
            "限定国家的多语种问题检索与排序融合",
            {
                "dimension": dimension,
                "keywords": kws[:6],
                "top_k": top_k,
                "query": query,
                "retrieval_version": RETRIEVAL_VERSION,
                "planning": planning,
            },
            f"命中 {len(scored)} 条,取前 {len(top)} 条: "
            + ("; ".join(f"{t.document_title[:24]} {t.label}" for t in top) or "无"),
            started,
            verified=bool(top) and not planning.get("error"),
        )
        return top

    async def query_kg_obligations(self, clause_ids: list[str]) -> list[dict[str, str]]:
        started = time.perf_counter()
        ids = [uuid.UUID(c) for c in clause_ids]
        edges = (
            list(
                (
                    await self.session.execute(
                        select(KgEdge)
                        .where(
                            KgEdge.evidence_clause_id.in_(ids),
                            KgEdge.country_code == self.country_code,
                            *active_edge_filters(),
                            KgEdge.review_status.in_(("approved", "pending_review")),
                            KgEdge.is_structural.is_(False),
                            KgEdge.judge_verdict == "yes",
                            KgEdge.extractor_model != "kg-rule-extractor-v1",
                        )
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            if ids
            else []
        )
        node_ids = {e.source_node_id for e in edges} | {e.target_node_id for e in edges}
        nodes = (
            {
                n.id: n
                for n in (await self.session.execute(select(KgNode).where(KgNode.id.in_(node_ids))))
                .scalars()
                .all()
            }
            if node_ids
            else {}
        )
        out = [
            {
                "clause_id": str(e.evidence_clause_id),
                "relation": e.relation_type,
                "source": nodes[e.source_node_id].name_zh if e.source_node_id in nodes else "",
                "target": nodes[e.target_node_id].name_zh if e.target_node_id in nodes else "",
                "review_status": e.review_status,
            }
            for e in edges
            if e.source_node_id in nodes
            and e.target_node_id in nodes
            and nodes[e.source_node_id].review_status in ("approved", "pending_review")
            and nodes[e.target_node_id].review_status in ("approved", "pending_review")
        ]
        self._record(
            "query_kg_obligations",
            "知识图谱六要素/十二关系联查",
            {"clause_count": len(ids)},
            f"关联到 {len(out)} 条语义边(图谱抽取结果,未复核的仅作参考)",
            started,
        )
        return out

    async def verify_clause_effective_date(self, cands: list[ClauseCandidate]) -> dict[str, Any]:
        started = time.perf_counter()
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for c in cands:
            if c.version_id in seen:
                continue
            seen.add(c.version_id)
            counts[c.legal_status] = counts.get(c.legal_status, 0) + 1
        unknown = counts.get("unknown", 0)
        self._record(
            "verify_clause_effective_date",
            "条款所属版本的现行状态核验",
            {"versions": len(seen)},
            f"{len(seen)} 个版本,现行状态: {counts};"
            + (f"其中 {unknown} 个官方未核实现行状态,不得视为已确认现行有效" if unknown else ""),
            started,
            verified=unknown == 0,
        )
        return counts


def verify_finding(finding: dict[str, Any], clause_text: str) -> tuple[bool, list[str]]:
    """程序化核验:引用必须是条款原文的连续子串,且结论中的数字必须出现在原文里。"""
    issues: list[str] = []
    quote = str(finding.get("quote") or "")
    if len(quote.strip()) < 15:
        issues.append("引用片段过短或为空")
    elif quote not in clause_text:
        issues.append("引用片段不是该条款原文的连续子串")
    obligation = str(finding.get("obligation") or "")
    for value, percent in sorted(tokens(obligation) - tokens(quote)):
        issues.append(f"结论中的数字「{value}{'%' if percent else ''}」未出现在所选引文中")
    for currency, value in sorted(amounts(obligation) - amounts(quote)):
        issues.append(f"币种/金额不一致：{currency} {value}")
    if dates(obligation) - dates(quote):
        issues.append("结论日期未获所选引文支持")
    return (not issues), issues


async def check_country_report_updates(
    session: AsyncSession, cited_version_ids: list[str]
) -> list[dict[str, Any]]:
    """若报告引用的文件已出现更新版本,返回受影响的引用。"""
    if not cited_version_ids:
        return []
    ids = [uuid.UUID(v) for v in cited_version_ids]
    cited = list(
        (
            await session.execute(
                select(
                    DocumentVersion.id, DocumentVersion.document_id, DocumentVersion.version_no
                ).where(DocumentVersion.id.in_(ids))
            )
        ).all()
    )
    out: list[dict[str, Any]] = []
    for vid, doc_id, ver_no in cited:
        newest = (
            await session.execute(
                select(func.max(DocumentVersion.version_no)).where(
                    DocumentVersion.document_id == doc_id
                )
            )
        ).scalar_one()
        if newest and newest > ver_no:
            out.append(
                {
                    "version_id": str(vid),
                    "document_id": str(doc_id),
                    "cited_version": ver_no,
                    "latest_version": int(newest),
                }
            )
    return out
