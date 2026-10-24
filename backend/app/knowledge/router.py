from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_user_side
from app.iam.models import Role
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_available, document_is_held
from app.knowledge.extractor import (
    COUNTRY_NAMES,
    extract_knowledge_for_country,
    seed_glossary_and_review_points,
)
from app.knowledge.graph_evidence import active_edge_filters, evidence_attributes
from app.knowledge.models import (
    ClauseTranslation,
    GlossaryTerm,
    KgEdge,
    KgNode,
    NodeType,
    RelationType,
    ReviewPoint,
    TranslationReviewStatus,
)
from app.knowledge.schemas import (
    ClauseTranslationOut,
    EdgeReviewIn,
    GlossaryTermCreate,
    GlossaryTermOut,
    GlossaryTermReviewIn,
    GraphPayloadOut,
    KgEdgeOut,
    KgNodeOut,
    RegulationClauseItemOut,
    RegulationDetailOut,
    RegulationSummaryOut,
    ReviewPointCreate,
    ReviewPointDecideIn,
    ReviewPointImportIn,
    ReviewPointOut,
    TranslationReviewIn,
)
from app.knowledge.translator import (
    translate_clause,
    translate_document_latest_version,
    verify_translation,
)
from app.sources.models import SourceSite

AdminOrReviewerDep = Annotated[Principal, Depends(require_admin_side)]
UserDep = Annotated[Principal, Depends(require_user_side)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

admin_router = APIRouter(prefix="/api/admin/knowledge", tags=["knowledge-admin"])
user_router = APIRouter(prefix="/api/knowledge", tags=["knowledge-user"])


@admin_router.get("/parsed-graph-revisions")
async def parsed_graph_revisions(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str = Query(..., pattern="^(SG|EG|TN|MA|LB)$"),
) -> list[dict]:
    """Page-based graph proposals are for review, never active report/legal evidence."""
    from app.knowledge.history import payload_hash
    from app.knowledge.models import KnowledgeAuditRecord

    rows = (
        await session.scalars(
            select(KnowledgeAuditRecord)
            .join(Clause, Clause.id == KnowledgeAuditRecord.target_id)
            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                KnowledgeAuditRecord.action == "kg_parsed_source_result",
                Document.country_code == country_code,
            )
            .order_by(KnowledgeAuditRecord.created_at.desc())
        )
    ).all()
    return [
        dict(
            id=str(row.id),
            clause_id=str(row.target_id),
            content_hash=row.content_hash,
            payload=row.payload,
        )
        for row in rows
        if payload_hash(row.payload) == row.content_hash
    ]


def _status_badge(review_status: str) -> tuple[bool, str]:
    if review_status == TranslationReviewStatus.APPROVED.value:
        return True, "已复核(可作法律依据)"
    if review_status == TranslationReviewStatus.REJECTED.value:
        return False, "已驳回(待重译)"
    if review_status == TranslationReviewStatus.STALE.value:
        return False, "法规已修订，旧译文待重核"
    if review_status == TranslationReviewStatus.TRANSLATION_FAILED.value:
        return False, "翻译校验未通过(待重译)"
    if review_status == TranslationReviewStatus.NOT_TRANSLATED.value:
        return False, "未翻译"
    if review_status == TranslationReviewStatus.SUPERSEDED.value:
        return False, "旧版已作废"
    return False, "机器翻译，待核对"


async def _build_translation_outs(
    session: AsyncSession,
    rows: list[tuple[ClauseTranslation, Clause, Document]],
) -> list[ClauseTranslationOut]:
    from app.knowledge.models import KnowledgeAuditRecord

    contexts, parse_revisions = {}, {}
    if rows:
        for record in (
            await session.scalars(
                select(KnowledgeAuditRecord)
                .where(
                    KnowledgeAuditRecord.target_id.in_([clause.id for _, clause, _ in rows]),
                    KnowledgeAuditRecord.action.in_(
                        ("translation_context_revision", "source_parse_revision")
                    ),
                )
                .order_by(KnowledgeAuditRecord.created_at.desc())
            )
        ).all():
            if record.action == "source_parse_revision":
                parse_revisions.setdefault(
                    record.target_id,
                    dict(
                        id=str(record.id),
                        review_status=record.payload["review_status"],
                        content_kind=record.payload["content_kind"],
                        active=False,
                        usable_as_legal_basis=False,
                    ),
                )
                continue
            contexts.setdefault(
                record.target_id,
                dict(
                    id=str(record.id),
                    quality_check_passed=record.payload["quality_check_passed"],
                    review_status=record.payload["review_status"],
                    standalone_translation=False,
                ),
            )
    out: list[ClauseTranslationOut] = []
    for tr, clause, doc in rows:
        usable, badge = _status_badge(tr.review_status)
        usable = usable and tr.is_current and tr.quality_check_passed
        out.append(
            ClauseTranslationOut(
                source_context_revision=contexts.get(clause.id),
                source_parse_revision=parse_revisions.get(clause.id),
                is_current=tr.is_current,
                id=tr.id,
                clause_id=clause.id,
                document_id=doc.id,
                country_code=doc.country_code,
                document_title=doc.title,
                clause_number=clause.clause_number,
                heading=clause.heading,
                section_path=clause.section_path,
                original_text=clause.original_text,
                translated_text=tr.translated_text,
                engine=tr.engine,
                model_name=tr.model_name,
                prompt_version=tr.prompt_version,
                tokens_in=int(tr.tokens_in or 0),
                tokens_out=int(tr.tokens_out or 0),
                duration_ms=int(tr.duration_ms or 0),
                glossary_hits=tr.glossary_hits or [],
                unmatched_terms=tr.unmatched_terms or [],
                quality_check_passed=tr.quality_check_passed,
                quality_issues=tr.quality_issues or [],
                review_status=tr.review_status,
                status=tr.review_status,
                usable_as_legal_basis=usable,
                status_badge=badge,
                reviewer_note=tr.reviewer_note,
                reviewed_at=tr.reviewed_at,
            )
        )
    return out


_DOC_TYPE_ZH: dict[str, str] = {
    "act": "法律 (Act / Loi)",
    "regulation": "行政法规与规则 (Regulation / Rules)",
    "subsidiary": "附属立法与通告 (Subsidiary Legislation)",
    "guidance": "监管指南与框架 (Advisory Guidelines / Framework)",
    "guideline": "监管指南与框架 (Advisory Guidelines / Framework)",
}


def _doc_type_zh(doc_type: str | None, title: str = "") -> str:
    raw = (doc_type or "").lower().strip()
    t_low = title.lower()
    if "guideline" in t_low or "guide" in t_low or "framework" in t_low:
        return "监管指南与框架 (Guidelines / Framework)"
    if "rules" in t_low or "regulations" in t_low or "notification" in t_low or "order" in t_low:
        return "附属条例与实施规则 (Regulations / Rules)"
    return _DOC_TYPE_ZH.get(raw, "法律与核心制度 (Act / Law)")


def _compute_eight_attributes(
    country_code: str,
    title: str | None = None,
    rel_types: set[str] | None = None,
    data_names: list[str] | None = None,
    obl_names: list[str] | None = None,
) -> dict[str, str]:
    """No country/title templates: unknown dimensions must remain explicitly unknown.

    Data/obligation names alone do not establish industry, retention, penalties or admission rules.
    Evidence-backed attributes are assembled from actual current graph edges by the caller.
    """
    return {
        key: "待核查（尚无经核验的条款依据）"
        for key in (
            "applicable_industry",
            "data_scope",
            "cross_border_condition",
            "retention_period",
            "authorization_requirement",
            "penalty_amount",
            "violation_level",
            "admission_checklist",
        )
    }


async def _build_graph_payload(
    session: AsyncSession,
    country_code: str,
    only_approved: bool = False,
    user_view: bool = False,
) -> GraphPayloadOut:
    cc = country_code.upper()
    node_stmt = (
        select(KgNode)
        .where(
            KgNode.country_code == cc,
            KgNode.review_status != "superseded",
            or_(
                KgNode.document_id.is_(None),
                KgNode.document_id.in_(select(Document.id).where(document_available())),
            ),
        )
        .order_by(KgNode.node_type)
    )
    if only_approved:
        node_stmt = node_stmt.where(KgNode.review_status == "approved")
    elif user_view:
        node_stmt = node_stmt.where(KgNode.review_status.in_(("approved", "pending_review")))
    nodes = list((await session.execute(node_stmt)).scalars().all())
    node_by_id = {n.id: n for n in nodes}

    edge_stmt = (
        select(KgEdge)
        .where(
            KgEdge.country_code == cc,
            *active_edge_filters(),
        )
        .order_by(KgEdge.relation_type)
    )
    if only_approved:
        edge_stmt = edge_stmt.where(KgEdge.review_status == "approved")
    elif user_view:
        edge_stmt = edge_stmt.where(KgEdge.review_status.in_(("approved", "pending_review")))
    edges = list((await session.execute(edge_stmt)).scalars().all())

    superseded_edges_count = int(
        (
            await session.execute(
                select(func.count(KgEdge.id)).where(
                    KgEdge.country_code == cc,
                    KgEdge.review_status == "superseded",
                )
            )
        ).scalar_one()
        or 0
    )

    clause_ids = {e.evidence_clause_id for e in edges}
    clause_map: dict[uuid.UUID, tuple[str | None, str | None, str]] = {}
    clause_provenance: dict[uuid.UUID, dict] = {}
    if clause_ids:
        c_rows = (
            await session.execute(
                select(
                    Clause.id,
                    Clause.clause_number,
                    Document.title,
                    Clause.original_text,
                    DocumentVersion.id,
                    DocumentVersion.content_hash,
                    Document.source_url,
                )
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Clause.id.in_(clause_ids))
            )
        ).all()
        for cid, cnum, dtitle, orig_txt, version_id, content_hash, source_url in c_rows:
            clause_map[cid] = (cnum, dtitle, orig_txt or "")
            clause_provenance[cid] = {
                "evidence_version_id": version_id,
                "evidence_content_hash": content_hash,
                "official_source_url": source_url,
            }

    node_counts = {t.value: 0 for t in NodeType}
    for n in nodes:
        node_counts[n.node_type] = node_counts.get(n.node_type, 0) + 1

    edge_counts = {r.value: 0 for r in RelationType}
    edge_outs: list[KgEdgeOut] = []
    connected_node_ids: set[uuid.UUID] = set()
    node_rels: dict[uuid.UUID, set[str]] = {}
    node_data_names: dict[uuid.UUID, list[str]] = {}
    node_obl_names: dict[uuid.UUID, list[str]] = {}
    quote_mismatch_count = 0

    for e in edges:
        src = node_by_id.get(e.source_node_id)
        dst = node_by_id.get(e.target_node_id)
        if not src or not dst:
            continue
        connected_node_ids.add(src.id)
        connected_node_ids.add(dst.id)
        node_rels.setdefault(src.id, set()).add(e.relation_type)
        node_rels.setdefault(dst.id, set()).add(e.relation_type)
        if dst.node_type == NodeType.DATA_CATEGORY.value:
            node_data_names.setdefault(src.id, []).append(dst.name_zh)
        if dst.node_type == NodeType.OBLIGATION.value:
            node_obl_names.setdefault(src.id, []).append(dst.name_zh)

        edge_counts[e.relation_type] = edge_counts.get(e.relation_type, 0) + 1
        cnum, dtitle, orig_txt = clause_map.get(e.evidence_clause_id, (None, None, ""))
        if not e.evidence_quote or (orig_txt and e.evidence_quote not in orig_txt):
            quote_mismatch_count += 1
        edge_outs.append(
            KgEdgeOut(
                **clause_provenance.get(e.evidence_clause_id, {}),
                id=e.id,
                country_code=e.country_code,
                source_node_id=e.source_node_id,
                source_name_zh=src.name_zh,
                source_node_type=src.node_type,
                target_node_id=e.target_node_id,
                target_name_zh=dst.name_zh,
                target_node_type=dst.node_type,
                relation_type=e.relation_type,
                evidence_clause_id=e.evidence_clause_id,
                evidence_quote=e.evidence_quote,
                clause_number=cnum,
                document_title=dtitle,
                extractor_model=e.extractor_model,
                confidence=float(getattr(e, "confidence", 0.85)),
                judge_verdict=str(getattr(e, "judge_verdict", "yes")),
                judge_reason=getattr(e, "judge_reason", None),
                is_structural=bool(getattr(e, "is_structural", False)),
                review_status=e.review_status,
                usable_as_legal_basis=(
                    e.review_status == "approved"
                    and not e.is_structural
                    and e.judge_verdict == "yes"
                ),
            )
        )

    # 统计本国条款总数、未挂接义务/关系的条款数、OCR/阿语需人工核对条款数
    total_clauses = int(
        (
            await session.execute(
                select(func.count(Clause.id))
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.country_code == cc, DocumentVersion.state == "parsed")
            )
        ).scalar_one()
        or 0
    )
    ocr_or_rtl_clauses = int(
        (
            await session.execute(
                select(func.count(Clause.id))
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.country_code == cc,
                    DocumentVersion.state == "parsed",
                    or_(
                        DocumentVersion.text_source == "ocr",
                        Document.language == "ar",
                    ),
                )
            )
        ).scalar_one()
        or 0
    )
    isolated_ids = [str(n.id) for n in nodes if n.id not in connected_node_ids]
    pending_edges = sum(1 for e in edge_outs if e.review_status == "pending_review")
    approved_edges = sum(1 for e in edge_outs if e.review_status == "approved")
    stale_or_rejected = sum(1 for e in edge_outs if e.review_status in ("stale", "rejected"))
    structural_edges = sum(1 for e in edge_outs if e.is_structural)
    semantic_edges = sum(1 for e in edge_outs if not e.is_structural)
    semantic_unjudged = sum(
        1 for e in edge_outs if not e.is_structural and e.judge_verdict != "yes"
    )
    low_confidence = sum(
        1
        for e in edge_outs
        if not e.is_structural and (e.confidence < 0.75 or e.judge_verdict == "partial")
    )

    node_outs: list[KgNodeOut] = []
    for n in nodes:
        eight_attrs = _compute_eight_attributes(
            cc,
            title=n.name_orig or n.name_zh,
            rel_types=node_rels.get(n.id),
            data_names=node_data_names.get(n.id),
            obl_names=node_obl_names.get(n.id),
        )
        actual, provenance = evidence_attributes(edges, node_by_id, clause_map, node_id=n.id)
        merged_attrs = {
            **(n.attributes or {}),
            **eight_attrs,
            **actual,
            "attribute_evidence": provenance,
        }
        nout = KgNodeOut.model_validate(n)
        nout.attributes = merged_attrs
        node_outs.append(nout)

    doc_scope_rows = (
        await session.execute(
            select(Document.relevance, func.count(Document.id))
            .where(Document.country_code == cc)
            .group_by(Document.relevance)
        )
    ).all()
    doc_scope = {str(r): int(c) for r, c in doc_scope_rows}
    docs_total = sum(doc_scope.values())
    docs_out_of_scope = doc_scope.get("out_of_scope", 0) + doc_scope.get("irrelevant", 0)
    source_held_count = int(
        await session.scalar(
            select(func.count(Document.id)).where(
                Document.country_code == cc,
                ~document_available(),
            )
        )
        or 0
    )

    return GraphPayloadOut(
        country_code=cc,
        node_counts_by_type=node_counts,
        edge_counts_by_relation=edge_counts,
        quality_summary={
            "documents_total": docs_total,
            "documents_core": doc_scope.get("relevant", 0),
            "documents_related": doc_scope.get("related", 0),
            "documents_out_of_scope": docs_out_of_scope,
            "documents_extracted": max(0, docs_total - docs_out_of_scope - source_held_count),
            "source_integrity_held_documents": source_held_count,
            "structural_edges": structural_edges,
            "semantic_edges": semantic_edges,
            "semantic_unjudged": semantic_unjudged,
            "low_confidence": low_confidence,
            "quote_mismatch": quote_mismatch_count,
            "superseded_edges_count": superseded_edges_count,
            "isolated_nodes_count": len(isolated_ids),
            "isolated_node_ids": isolated_ids,
            "total_clauses_count": total_clauses,
            "linked_clauses_count": len(clause_ids),
            "unlinked_clauses_count": max(0, total_clauses - len(clause_ids)),
            "pending_review_edges_count": pending_edges,
            "approved_edges_count": approved_edges,
            "stale_or_rejected_edges_count": stale_or_rejected,
            "quote_mismatch_edges_count": quote_mismatch_count,
            "ocr_or_rtl_clauses_count": ocr_or_rtl_clauses,
        },
        nodes=node_outs,
        edges=edge_outs,
    )


# ==================== 管理端与律所复核端接口 ====================


@admin_router.post("/extract")
async def trigger_country_extraction(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    translate_sample_per_doc: int = Query(default=0, ge=0, le=50),
    max_clauses_per_doc: int | None = Query(default=None, ge=1, le=500),
) -> dict:
    """一键初始化术语库/解读要点,并对指定国家执行六要素+十二关系知识图谱抽取与核心条款翻译。"""
    return await extract_knowledge_for_country(
        session,
        country_code.upper(),
        translate_sample_per_doc=translate_sample_per_doc,
        max_clauses_per_doc=max_clauses_per_doc,
    )


@admin_router.get("/glossary", response_model=list[GlossaryTermOut])
async def list_glossary_terms(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str | None = None,
    review_status: str | None = None,
) -> list[GlossaryTerm]:
    await seed_glossary_and_review_points(session)
    stmt = select(GlossaryTerm).order_by(
        GlossaryTerm.priority_level,
        GlossaryTerm.country_code.nulls_last(),
        GlossaryTerm.concept_code,
    )
    if country_code:
        cc = country_code.upper()
        stmt = stmt.where(or_(GlossaryTerm.country_code == cc, GlossaryTerm.country_code.is_(None)))
    if review_status:
        stmt = stmt.where(GlossaryTerm.review_status == review_status)
    return list((await session.execute(stmt)).scalars().all())


@admin_router.post("/glossary", response_model=GlossaryTermOut, status_code=status.HTTP_201_CREATED)
async def create_glossary_term(
    body: GlossaryTermCreate,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> GlossaryTerm:
    term = GlossaryTerm(
        concept_code=body.concept_code.strip(),
        country_code=body.country_code.upper() if body.country_code else None,
        language=body.language.lower().strip(),
        source_term=body.source_term.strip(),
        target_zh=body.target_zh.strip(),
        priority_level=body.priority_level,
        statutory_definition=body.statutory_definition,
        source_reference=body.source_reference,
        evidence_clause_id=body.evidence_clause_id,
        review_status="approved"
        if user.role in (Role.PLATFORM_ADMIN, Role.REVIEWER)
        else "pending",
        reviewed_by=user.user_id,
        reviewed_at=datetime.now(UTC),
    )
    session.add(term)
    await session.commit()
    await session.refresh(term)
    return term


@admin_router.post("/glossary/{term_id}/review", response_model=GlossaryTermOut)
async def review_glossary_term(
    term_id: uuid.UUID,
    body: GlossaryTermReviewIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> GlossaryTerm:
    term = await session.get(GlossaryTerm, term_id)
    if term is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="术语不存在")
    term.review_status = body.decision
    if body.target_zh:
        term.target_zh = body.target_zh.strip()
    if body.statutory_definition is not None:
        term.statutory_definition = body.statutory_definition
    term.reviewed_by = user.user_id
    term.reviewed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(term)
    return term


@admin_router.get("/translations", response_model=list[ClauseTranslationOut])
async def list_translations(
    response: Response,
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str | None = None,
    review_status: str | None = None,
    document_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_history: bool = False,
) -> list[ClauseTranslationOut]:
    stmt = (
        select(ClauseTranslation, Clause, Document)
        .join(Clause, Clause.id == ClauseTranslation.clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .order_by(ClauseTranslation.updated_at.desc(), Clause.ordinal, ClauseTranslation.id)
    )
    if country_code:
        stmt = stmt.where(Document.country_code == country_code.upper())
    if review_status:
        stmt = stmt.where(ClauseTranslation.review_status == review_status)
    elif not include_history:
        stmt = stmt.where(
            ClauseTranslation.review_status != TranslationReviewStatus.SUPERSEDED.value
        )
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    if not include_history and review_status not in (
        "translation_failed",
        "not_translated",
        "superseded",
    ):
        stmt = stmt.where(ClauseTranslation.is_current.is_(True))
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    response.headers["X-Total-Count"] = str(total)
    rows = list((await session.execute(stmt.offset(offset).limit(limit))).all())
    return await _build_translation_outs(session, rows)


@admin_router.post("/clauses/{clause_id}/translate", response_model=ClauseTranslationOut)
async def trigger_clause_translate(
    clause_id: uuid.UUID,
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> ClauseTranslationOut:
    row = (
        await session.execute(
            select(Clause, Document)
            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Clause.id == clause_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="条款不存在")
    clause, doc = row
    if await document_is_held(session, doc.id):
        raise HTTPException(409, "原文绑定异常已隔离，请先核查来源后再翻译")
    tr = await translate_clause(session, clause, doc.country_code)
    await session.commit()
    outs = await _build_translation_outs(session, [(tr, clause, doc)])
    return outs[0]


@admin_router.post("/documents/{document_id}/translate")
async def trigger_document_translate(
    document_id: uuid.UUID,
    _: AdminOrReviewerDep,
    session: SessionDep,
    limit_clauses: int | None = Query(default=None, ge=1, le=500),
) -> dict:
    if await document_is_held(session, document_id):
        raise HTTPException(409, "原文绑定异常已隔离，请先核查来源后再翻译")
    trs = await translate_document_latest_version(session, document_id, limit_clauses=limit_clauses)
    return {
        "document_id": str(document_id),
        "attempted_count": len(trs),
        "translated_count": sum(tr.quality_check_passed for tr in trs),
        "failed_count": sum(not tr.quality_check_passed for tr in trs),
        "limited": limit_clauses is not None,
    }


@admin_router.post("/translations/{translation_id}/review", response_model=ClauseTranslationOut)
async def review_clause_translation(
    translation_id: uuid.UUID,
    body: TranslationReviewIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> ClauseTranslationOut:
    row = (
        await session.execute(
            select(ClauseTranslation, Clause, Document)
            .join(Clause, Clause.id == ClauseTranslation.clause_id)
            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(ClauseTranslation.id == translation_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="翻译记录不存在")
    tr, clause, doc = row
    from app.knowledge.history import snapshot

    if body.decision == "approved" and await document_is_held(session, doc.id):
        raise HTTPException(409, "来源绑定异常尚未解决，不可审批为法律依据")
    proposed_text = (body.translated_text or tr.translated_text).strip()
    passed, issues, checked_hits = verify_translation(
        clause.original_text, proposed_text, tr.glossary_hits or []
    )
    if body.decision == "approved" and (not passed or tr.review_status in ("superseded", "stale")):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该译文不能审批通过：请修正校验问题，历史/失效版本须另建修订",
                "issues": issues,
            },
        )
    snapshot(session, tr, "before_human_review", user.user_id)
    if proposed_text != tr.translated_text:
        # Corrections are new revisions. Approval of the old text is never inherited.
        old = tr
        tr = ClauseTranslation(
            clause_id=old.clause_id,
            translated_text=proposed_text,
            engine="human_revision",
            model_name=old.model_name,
            prompt_version="human-edit",
            glossary_hits=checked_hits,
            unmatched_terms=old.unmatched_terms,
            quality_check_passed=passed,
            quality_issues=issues,
            is_current=old.is_current,
        )
        old.is_current = False
        session.add(tr)
        await session.flush()
    tr.quality_check_passed = passed
    tr.quality_issues = issues
    tr.glossary_hits = checked_hits
    tr.review_status = body.decision
    tr.reviewer_id = user.user_id
    tr.reviewer_note = body.reviewer_note
    tr.reviewed_at = datetime.now(UTC)
    tr.updated_at = datetime.now(UTC)
    snapshot(session, tr, "after_human_review", user.user_id)
    await session.commit()
    outs = await _build_translation_outs(session, [(tr, clause, doc)])
    return outs[0]


@admin_router.get("/review-points", response_model=list[ReviewPointOut])
async def list_review_points(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
) -> list[ReviewPoint]:
    await seed_glossary_and_review_points(session)
    stmt = select(ReviewPoint).order_by(ReviewPoint.country_code, ReviewPoint.point_code)
    if country_code:
        stmt = stmt.where(ReviewPoint.country_code == country_code.upper())
    if status_filter:
        stmt = stmt.where(ReviewPoint.status == status_filter)
    return list((await session.execute(stmt)).scalars().all())


@admin_router.post(
    "/review-points", response_model=ReviewPointOut, status_code=status.HTTP_201_CREATED
)
async def create_review_point(
    body: ReviewPointCreate,
    _: AdminOrReviewerDep,
    session: SessionDep,
) -> ReviewPoint:
    rp = ReviewPoint(
        country_code=body.country_code.upper(),
        point_code=body.point_code.strip(),
        title=body.title.strip(),
        question=body.question.strip(),
        impact_on_conclusion=body.impact_on_conclusion.strip(),
        provisional_interpretation=body.provisional_interpretation.strip(),
        clause_id=body.clause_id,
        term_id=body.term_id,
    )
    session.add(rp)
    await session.commit()
    await session.refresh(rp)
    return rp


@admin_router.post("/review-points/{point_id}/decide", response_model=ReviewPointOut)
async def decide_review_point(
    point_id: uuid.UUID,
    body: ReviewPointDecideIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> ReviewPoint:
    rp = await session.get(ReviewPoint, point_id)
    if rp is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="确认单条目不存在")
    rp.status = body.status
    rp.lawyer_reply = body.lawyer_reply.strip()
    rp.decided_by = user.user_id
    rp.decided_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(rp)
    return rp


@admin_router.get("/review-points/export")
async def export_review_points(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str | None = None,
) -> dict:
    """导出《解读要点确认单》(含 Markdown 表格与 JSON 结构),供外部合作律所离线填写并回传。"""
    await seed_glossary_and_review_points(session)
    stmt = select(ReviewPoint).order_by(ReviewPoint.country_code, ReviewPoint.point_code)
    if country_code:
        stmt = stmt.where(ReviewPoint.country_code == country_code.upper())
    points = list((await session.execute(stmt)).scalars().all())

    md_lines = [
        "# 跨境合规法规解读要点确认单(供合作律所复核)",
        "",
        "> 说明:本确认单仅汇总法规未明确定义、官方无现成解释、但直接影响结论的少数关键问题。",
        "",
        "| 编号 | 国家 | 议题 | 待确认问题 | 对结论影响 | 暂拟口径 | 状态 | 律所意见 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    items_json: list[dict] = []
    for p in points:
        md_lines.append(
            f"| {p.point_code} | {p.country_code} | {p.title} | {p.question} | "
            f"{p.impact_on_conclusion} | {p.provisional_interpretation} | "
            f"{p.status} | {p.lawyer_reply or '待填写'} |"
        )
        items_json.append(
            {
                "point_code": p.point_code,
                "country_code": p.country_code,
                "title": p.title,
                "question": p.question,
                "impact_on_conclusion": p.impact_on_conclusion,
                "provisional_interpretation": p.provisional_interpretation,
                "status": p.status,
                "lawyer_reply": p.lawyer_reply,
            }
        )
    return {
        "markdown": "\n".join(md_lines) + "\n",
        "items": items_json,
    }


@admin_router.post("/review-points/import")
async def import_review_point_decisions(
    body: ReviewPointImportIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> dict:
    """批量导入律所填写的《解读要点确认单》回执。"""
    updated = 0
    not_found: list[str] = []
    for item in body.items:
        rp = (
            await session.execute(
                select(ReviewPoint).where(ReviewPoint.point_code == item.point_code)
            )
        ).scalar_one_or_none()
        if rp is None:
            not_found.append(item.point_code)
            continue
        reply = (item.lawyer_reply or "").strip() or rp.provisional_interpretation
        rp.status = "confirmed" if item.status == "open" and item.lawyer_reply else item.status
        rp.lawyer_reply = reply
        rp.decided_by = user.user_id
        rp.decided_at = datetime.now(UTC)
        updated += 1
    await session.commit()
    return {"updated": updated, "not_found": not_found}


@admin_router.get("/graph", response_model=GraphPayloadOut)
async def get_admin_knowledge_graph(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    only_approved: bool = False,
) -> GraphPayloadOut:
    return await _build_graph_payload(session, country_code, only_approved=only_approved)


@admin_router.get("/history/{target_id}")
async def knowledge_history(
    target_id: uuid.UUID,
    _: AdminOrReviewerDep,
    session: SessionDep,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    from app.knowledge.history import record_payload
    from app.knowledge.models import KnowledgeAuditRecord, KnowledgeChangeProposal

    records = (
        await session.scalars(
            select(KnowledgeAuditRecord)
            .where(KnowledgeAuditRecord.target_id == target_id)
            .order_by(KnowledgeAuditRecord.created_at.desc(), KnowledgeAuditRecord.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    proposals = (
        await session.scalars(
            select(KnowledgeChangeProposal)
            .where(KnowledgeChangeProposal.target_id == target_id)
            .order_by(KnowledgeChangeProposal.created_at.desc(), KnowledgeChangeProposal.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "history": [record_payload(row) for row in records],
        "proposals": [record_payload(row) for row in proposals],
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/graph-runs")
async def graph_runs(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    from app.knowledge.history import record_payload
    from app.knowledge.models import KnowledgeAuditRecord

    country_id = uuid.uuid5(uuid.NAMESPACE_URL, "cyshuhe:kg:" + country_code.upper())
    rows = (
        await session.scalars(
            select(KnowledgeAuditRecord)
            .where(
                KnowledgeAuditRecord.target_id == country_id,
                KnowledgeAuditRecord.action == "graph_rebuild_result",
            )
            .order_by(KnowledgeAuditRecord.created_at.desc(), KnowledgeAuditRecord.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [record_payload(row) for row in rows]


@admin_router.post("/edges/{edge_id}/review", response_model=KgEdgeOut)
async def review_knowledge_edge(
    edge_id: uuid.UUID,
    body: EdgeReviewIn,
    user: AdminOrReviewerDep,
    session: SessionDep,
) -> KgEdgeOut:
    edge = await session.get(KgEdge, edge_id)
    if edge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="图谱关系边不存在")
    from app.knowledge.history import snapshot

    clause = await session.get(Clause, edge.evidence_clause_id)
    from app.ingestion.safety import held_citation_ids

    if body.decision == "approved" and await held_citation_ids(session, [edge.evidence_clause_id]):
        raise HTTPException(409, "原文绑定异常已隔离，不能批准为法律依据")
    if body.decision == "approved" and (
        edge.review_status in ("superseded", "stale")
        or edge.extractor_model == "kg-rule-extractor-v1"
        or not clause
        or not edge.evidence_quote
        or edge.evidence_quote not in clause.original_text
        or (not edge.is_structural and edge.judge_verdict != "yes")
        or (edge.is_structural and edge.judge_verdict != "structural")
    ):
        raise HTTPException(status_code=409, detail="关系证据/裁判未通过或已失效；请先建立有效修订")
    snapshot(session, edge, "before_human_review", user.user_id)
    edge.review_status = body.decision
    edge.reviewed_by = user.user_id
    edge.reviewed_at = datetime.now(UTC)
    snapshot(session, edge, "after_human_review", user.user_id)
    await session.commit()
    src = await session.get(KgNode, edge.source_node_id)
    dst = await session.get(KgNode, edge.target_node_id)
    clause = await session.get(Clause, edge.evidence_clause_id)
    return KgEdgeOut(
        id=edge.id,
        country_code=edge.country_code,
        source_node_id=edge.source_node_id,
        source_name_zh=src.name_zh if src else "",
        source_node_type=src.node_type if src else "",
        target_node_id=edge.target_node_id,
        target_name_zh=dst.name_zh if dst else "",
        target_node_type=dst.node_type if dst else "",
        relation_type=edge.relation_type,
        evidence_clause_id=edge.evidence_clause_id,
        evidence_quote=edge.evidence_quote,
        clause_number=clause.clause_number if clause else None,
        document_title=None,
        extractor_model=edge.extractor_model,
        confidence=float(getattr(edge, "confidence", 0.85)),
        judge_verdict=str(getattr(edge, "judge_verdict", "yes")),
        judge_reason=getattr(edge, "judge_reason", None),
        is_structural=bool(getattr(edge, "is_structural", False)),
        review_status=edge.review_status,
        usable_as_legal_basis=(
            edge.review_status == "approved"
            and not edge.is_structural
            and edge.judge_verdict == "yes"
        ),
    )


# ==================== 用户端法规知识库浏览接口 ====================


@user_router.get("/overview")
async def get_knowledge_overview(
    _: UserDep,
    session: SessionDep,
) -> list[dict]:
    """按五国返回法规知识库统计概览(文档数、条款数、译文数、已复核可引用的法律依据数、图谱节点与关系边数)。"""
    results: list[dict] = []
    for cc, (name_zh, name_orig) in COUNTRY_NAMES.items():
        docs_count = (
            await session.execute(
                select(func.count(Document.id)).where(
                    Document.country_code == cc, document_available()
                )
            )
        ).scalar_one()
        clauses_count = (
            await session.execute(
                select(func.count(Clause.id))
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.country_code == cc, document_available())
            )
        ).scalar_one()
        tr_total = (
            await session.execute(
                select(func.count(ClauseTranslation.id))
                .join(Clause, Clause.id == ClauseTranslation.clause_id)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.country_code == cc,
                    document_available(),
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status.notin_(
                        ("superseded", "not_translated", "translation_failed", "rejected", "stale")
                    ),
                )
            )
        ).scalar_one()
        tr_approved = (
            await session.execute(
                select(func.count(ClauseTranslation.id))
                .join(Clause, Clause.id == ClauseTranslation.clause_id)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    Document.country_code == cc,
                    document_available(),
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status == "approved",
                )
            )
        ).scalar_one()
        nodes_count = (
            await session.execute(
                select(func.count(KgNode.id)).where(
                    KgNode.country_code == cc,
                    KgNode.review_status != "superseded",
                    or_(
                        KgNode.document_id.is_(None),
                        KgNode.document_id.in_(select(Document.id).where(document_available())),
                    ),
                )
            )
        ).scalar_one()
        edges_count = (
            await session.execute(
                select(func.count(KgEdge.id)).where(
                    KgEdge.country_code == cc,
                    *active_edge_filters(),
                )
            )
        ).scalar_one()
        results.append(
            {
                "country_code": cc,
                "country_name_zh": name_zh,
                "country_name_orig": name_orig,
                "documents_count": int(docs_count or 0),
                "clauses_count": int(clauses_count or 0),
                "translations_count": int(tr_total or 0),
                "approved_translations_count": int(tr_approved or 0),
                "kg_nodes_count": int(nodes_count or 0),
                "kg_edges_count": int(edges_count or 0),
            }
        )
    return results


@user_router.get("/clauses", response_model=list[ClauseTranslationOut])
async def search_knowledge_clauses(
    _: UserDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    keyword: str | None = None,
    only_approved: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ClauseTranslationOut]:
    """用户端检索法规条款与对照译文(未复核内容显式带机器翻译待核对标识)。"""
    stmt = (
        select(ClauseTranslation, Clause, Document)
        .join(Clause, Clause.id == ClauseTranslation.clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Document.country_code == country_code.upper(),
            document_available(),
            ClauseTranslation.is_current.is_(True),
            ClauseTranslation.review_status.notin_(
                ("superseded", "not_translated", "translation_failed", "rejected", "stale")
            ),
        )
        .order_by(Document.title, Clause.ordinal)
        .limit(limit)
    )
    if only_approved:
        stmt = stmt.where(ClauseTranslation.review_status == "approved")
    if keyword and keyword.strip():
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Clause.original_text.ilike(kw),
                ClauseTranslation.translated_text.ilike(kw),
                Document.title.ilike(kw),
            )
        )
    rows = list((await session.execute(stmt)).all())
    return await _build_translation_outs(session, rows)


@user_router.get("/graph", response_model=GraphPayloadOut)
async def get_user_knowledge_graph(
    _: UserDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    only_approved: bool = False,
) -> GraphPayloadOut:
    return await _build_graph_payload(
        session, country_code, only_approved=only_approved, user_view=True
    )


@user_router.get("/glossary", response_model=list[GlossaryTermOut])
async def get_user_glossary(
    _: UserDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
) -> list[GlossaryTerm]:
    cc = country_code.upper()
    stmt = (
        select(GlossaryTerm)
        .where(
            or_(GlossaryTerm.country_code == cc, GlossaryTerm.country_code.is_(None)),
            GlossaryTerm.review_status == "approved",
        )
        .order_by(GlossaryTerm.priority_level, GlossaryTerm.concept_code)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _build_regulation_summary(
    session: AsyncSession, doc: Document
) -> tuple[RegulationSummaryOut, DocumentVersion | None]:
    if await document_is_held(session, doc.id):
        raise HTTPException(409, "原文绑定异常已隔离，请管理员核实正式来源")
    versions = list(
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    latest = next((v for v in versions if v.state == "parsed"), versions[0] if versions else None)
    site = await session.get(SourceSite, doc.site_id)
    publisher = site.publisher if site else f"{doc.country_code} 官方发布机构"

    clauses_count = 0
    translated_count = 0
    edges_count = 0
    rel_types: set[str] = set()
    data_names: list[str] = []
    obl_names: list[str] = []

    if latest is not None:
        clauses_count = int(
            (
                await session.execute(
                    select(func.count(Clause.id)).where(Clause.version_id == latest.id)
                )
            ).scalar_one()
            or 0
        )
        translated_count = int(
            (
                await session.execute(
                    select(func.count(ClauseTranslation.id))
                    .join(Clause, Clause.id == ClauseTranslation.clause_id)
                    .where(
                        Clause.version_id == latest.id,
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
            ).scalar_one()
            or 0
        )

    inst_node = (
        await session.execute(
            select(KgNode)
            .where(KgNode.document_id == doc.id, KgNode.review_status != "superseded")
            .limit(1)
        )
    ).scalar_one_or_none()
    if inst_node is not None:
        linked_edges = list(
            (
                await session.execute(
                    select(KgEdge).where(
                        *active_edge_filters(),
                        or_(
                            KgEdge.source_node_id == inst_node.id,
                            KgEdge.target_node_id == inst_node.id,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        edges_count = len(linked_edges)
        target_ids = {e.target_node_id for e in linked_edges}
        if target_ids:
            tnodes = list(
                (await session.execute(select(KgNode).where(KgNode.id.in_(target_ids))))
                .scalars()
                .all()
            )
            for tn in tnodes:
                if tn.node_type == NodeType.DATA_CATEGORY.value:
                    data_names.append(tn.name_zh)
                elif tn.node_type == NodeType.OBLIGATION.value:
                    obl_names.append(tn.name_zh)
        for e in linked_edges:
            rel_types.add(e.relation_type)

    eight_attrs = _compute_eight_attributes(
        doc.country_code,
        title=doc.title,
        rel_types=rel_types,
        data_names=data_names,
        obl_names=obl_names,
    )
    if inst_node is not None and linked_edges:
        source_rows = (
            await session.execute(
                select(Clause.id, Clause.clause_number, Clause.original_text).where(
                    Clause.id.in_([e.evidence_clause_id for e in linked_edges])
                )
            )
        ).all()
        node_ids = {e.source_node_id for e in linked_edges} | {
            e.target_node_id for e in linked_edges
        }
        all_nodes = (await session.scalars(select(KgNode).where(KgNode.id.in_(node_ids)))).all()
        actual, _ = evidence_attributes(
            linked_edges,
            {n.id: n for n in all_nodes},
            {cid: (number, doc.title, original) for cid, number, original in source_rows},
            node_id=inst_node.id,
        )
        eight_attrs.update(actual)
    summary = RegulationSummaryOut(
        id=doc.id,
        country_code=doc.country_code,
        title=doc.title,
        document_type=doc.document_type,
        document_type_zh=_doc_type_zh(doc.document_type, doc.title),
        language=doc.language,
        source_url=doc.source_url,
        publisher=publisher,
        version_no=latest.version_no if latest else 1,
        legal_status=latest.legal_status if latest else "in_force",
        fetched_at=latest.fetched_at if latest else doc.discovered_at,
        sha256_hex=latest.content_hash if latest else None,
        clauses_count=clauses_count,
        translated_clauses_count=translated_count,
        edges_count=edges_count,
        eight_attributes=eight_attrs,
        version_count=len(versions) or 1,
    )
    return summary, latest


@user_router.get("/regulations", response_model=list[RegulationSummaryOut])
async def list_user_regulations(
    _: UserDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    keyword: str | None = None,
) -> list[RegulationSummaryOut]:
    """按国家返回法规文件目录（供用户端「国家 → 法律层级/监管机构 → 法规」分级树与列表使用）。"""
    cc = country_code.upper()
    stmt = (
        select(Document)
        .where(Document.country_code == cc, document_available())
        .order_by(Document.title)
    )
    if keyword and keyword.strip():
        stmt = stmt.where(Document.title.ilike(f"%{keyword.strip()}%"))
    docs = list((await session.execute(stmt)).scalars().all())
    results: list[RegulationSummaryOut] = []
    for doc in docs:
        summary, _ = await _build_regulation_summary(session, doc)
        if summary.clauses_count > 0:
            results.append(summary)
    return results


@user_router.get("/regulations/{document_id}", response_model=RegulationDetailOut)
async def get_user_regulation_detail(
    document_id: uuid.UUID,
    _: UserDep,
    session: SessionDep,
) -> RegulationDetailOut:
    """获取单部法规详情：基本信息、8项结构化属性、版本历史、左右双栏原文/中译对照条款（含关联要素与原文高亮片段）、以及单法规1跳关联图谱。"""
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="法规文件不存在")

    summary, latest = await _build_regulation_summary(session, doc)
    versions = list(
        (
            await session.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc())
            )
        )
        .scalars()
        .all()
    )
    ver_list = [
        {
            "version_no": v.version_no,
            "legal_status": v.legal_status,
            "fetched_at": v.fetched_at.isoformat() if v.fetched_at else None,
            "content_hash": v.content_hash,
            "text_source": v.text_source,
            "note": (
                "当前归档版本（法律有效状态另行核验）"
                if v.version_no == (latest.version_no if latest else 1)
                else "历史版本（支持版本差异对比）"
            ),
        }
        for v in versions
    ]

    clauses_out: list[RegulationClauseItemOut] = []
    if latest is not None:
        clauses = list(
            (
                await session.execute(
                    select(Clause)
                    .where(Clause.version_id == latest.id)
                    .order_by(Clause.ordinal)
                    .limit(80)
                )
            )
            .scalars()
            .all()
        )
        c_ids = [c.id for c in clauses]
        tr_map: dict[uuid.UUID, ClauseTranslation] = {}
        edge_by_clause: dict[uuid.UUID, list[dict]] = {}

        if c_ids:
            trs = list(
                (
                    await session.execute(
                        select(ClauseTranslation).where(
                            ClauseTranslation.clause_id.in_(c_ids),
                            ClauseTranslation.is_current.is_(True),
                            ClauseTranslation.review_status
                            != TranslationReviewStatus.SUPERSEDED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for tr in trs:
                tr_map[tr.clause_id] = tr

            edges = list(
                (
                    await session.execute(
                        select(KgEdge).where(
                            KgEdge.evidence_clause_id.in_(c_ids),
                            *active_edge_filters(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            node_ids = {e.source_node_id for e in edges} | {e.target_node_id for e in edges}
            n_map: dict[uuid.UUID, KgNode] = {}
            if node_ids:
                ns = list(
                    (await session.execute(select(KgNode).where(KgNode.id.in_(node_ids))))
                    .scalars()
                    .all()
                )
                n_map = {n.id: n for n in ns}

            for e in edges:
                dst = n_map.get(e.target_node_id)
                src = n_map.get(e.source_node_id)
                if not dst or not src:
                    continue
                edge_by_clause.setdefault(e.evidence_clause_id, []).append(
                    {
                        "edge_id": str(e.id),
                        "relation_type": e.relation_type,
                        "source_name_zh": src.name_zh,
                        "source_node_type": src.node_type,
                        "target_name_zh": dst.name_zh,
                        "target_node_type": dst.node_type,
                        "evidence_quote": e.evidence_quote,
                        "usable_as_legal_basis": e.review_status == "approved",
                    }
                )

        for c in clauses:
            tr = tr_map.get(c.id)
            if (
                tr is not None
                and tr.review_status not in ("not_translated", "translation_failed", "superseded")
                and (tr.translated_text or "").strip()
            ):
                usable, badge = _status_badge(tr.review_status)
                tr_text = tr.translated_text
                tr_status = tr.review_status
                g_hits = tr.glossary_hits or []
                u_terms = tr.unmatched_terms or []
            else:
                usable = False
                tr_status = tr.review_status if tr is not None else "not_translated"
                badge = "翻译校验未通过" if tr_status == "translation_failed" else "未翻译"
                tr_text = None
                g_hits = tr.glossary_hits or [] if tr is not None else []
                u_terms = tr.unmatched_terms or [] if tr is not None else []

            clauses_out.append(
                RegulationClauseItemOut(
                    clause_id=c.id,
                    ordinal=c.ordinal,
                    clause_number=c.clause_number,
                    heading=c.heading,
                    section_path=c.section_path,
                    original_text=c.original_text,
                    translated_text=tr_text,
                    translation_status=tr_status,
                    status_badge=badge,
                    usable_as_legal_basis=usable,
                    glossary_hits=g_hits,
                    unmatched_terms=u_terms,
                    linked_edges=edge_by_clause.get(c.id, []),
                )
            )

    # 构造以当前法规为中心的 1 跳关联图谱
    full_graph = await _build_graph_payload(session, doc.country_code, only_approved=False)
    inst_node_out = next((n for n in full_graph.nodes if n.document_id == doc.id), None)
    if inst_node_out is not None:
        sub_edges = [
            e
            for e in full_graph.edges
            if e.source_node_id == inst_node_out.id or e.target_node_id == inst_node_out.id
        ]
        keep_ids = {inst_node_out.id}
        for e in sub_edges:
            keep_ids.add(e.source_node_id)
            keep_ids.add(e.target_node_id)
        sub_nodes = [n for n in full_graph.nodes if n.id in keep_ids]
        sub_node_counts = {t.value: 0 for t in NodeType}
        for n in sub_nodes:
            sub_node_counts[n.node_type] = sub_node_counts.get(n.node_type, 0) + 1
        sub_edge_counts = {r.value: 0 for r in RelationType}
        for e in sub_edges:
            sub_edge_counts[e.relation_type] = sub_edge_counts.get(e.relation_type, 0) + 1
        mini_graph = GraphPayloadOut(
            country_code=doc.country_code,
            node_counts_by_type=sub_node_counts,
            edge_counts_by_relation=sub_edge_counts,
            quality_summary={},
            nodes=sub_nodes,
            edges=sub_edges,
        )
    else:
        mini_graph = full_graph

    return RegulationDetailOut(
        regulation=summary,
        versions=ver_list,
        clauses=clauses_out,
        mini_graph=mini_graph,
    )
