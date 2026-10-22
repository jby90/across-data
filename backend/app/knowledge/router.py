from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_user_side
from app.iam.models import Role
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.extractor import (
    COUNTRY_NAMES,
    extract_knowledge_for_country,
    seed_glossary_and_review_points,
)
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

AdminOrReviewerDep = Annotated[Principal, Depends(require_admin_side)]
UserDep = Annotated[Principal, Depends(require_user_side)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

admin_router = APIRouter(prefix="/api/admin/knowledge", tags=["knowledge-admin"])
user_router = APIRouter(prefix="/api/knowledge", tags=["knowledge-user"])


def _status_badge(review_status: str) -> tuple[bool, str]:
    if review_status == TranslationReviewStatus.APPROVED.value:
        return True, "已复核(可作法律依据)"
    if review_status == TranslationReviewStatus.REJECTED.value:
        return False, "已驳回(待重译)"
    if review_status == TranslationReviewStatus.STALE.value:
        return False, "法规已修订，旧译文待重核"
    return False, "机器翻译，待核对"


async def _build_translation_outs(
    session: AsyncSession,
    rows: list[tuple[ClauseTranslation, Clause, Document]],
) -> list[ClauseTranslationOut]:
    out: list[ClauseTranslationOut] = []
    for tr, clause, doc in rows:
        usable, badge = _status_badge(tr.review_status)
        out.append(
            ClauseTranslationOut(
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
                glossary_hits=tr.glossary_hits or [],
                unmatched_terms=tr.unmatched_terms or [],
                quality_check_passed=tr.quality_check_passed,
                quality_issues=tr.quality_issues or [],
                review_status=tr.review_status,
                usable_as_legal_basis=usable,
                status_badge=badge,
                reviewer_note=tr.reviewer_note,
                reviewed_at=tr.reviewed_at,
            )
        )
    return out


async def _build_graph_payload(
    session: AsyncSession, country_code: str, only_approved: bool = False
) -> GraphPayloadOut:
    cc = country_code.upper()
    node_stmt = select(KgNode).where(KgNode.country_code == cc).order_by(KgNode.node_type)
    if only_approved:
        node_stmt = node_stmt.where(KgNode.review_status == "approved")
    nodes = list((await session.execute(node_stmt)).scalars().all())
    node_by_id = {n.id: n for n in nodes}

    edge_stmt = select(KgEdge).where(KgEdge.country_code == cc).order_by(KgEdge.relation_type)
    if only_approved:
        edge_stmt = edge_stmt.where(KgEdge.review_status == "approved")
    edges = list((await session.execute(edge_stmt)).scalars().all())

    clause_ids = {e.evidence_clause_id for e in edges}
    clause_map: dict[uuid.UUID, tuple[str | None, str | None]] = {}
    if clause_ids:
        c_rows = (
            await session.execute(
                select(Clause.id, Clause.clause_number, Document.title)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Clause.id.in_(clause_ids))
            )
        ).all()
        for cid, cnum, dtitle in c_rows:
            clause_map[cid] = (cnum, dtitle)

    node_counts = {t.value: 0 for t in NodeType}
    for n in nodes:
        node_counts[n.node_type] = node_counts.get(n.node_type, 0) + 1

    edge_counts = {r.value: 0 for r in RelationType}
    edge_outs: list[KgEdgeOut] = []
    for e in edges:
        src = node_by_id.get(e.source_node_id)
        dst = node_by_id.get(e.target_node_id)
        if not src or not dst:
            continue
        edge_counts[e.relation_type] = edge_counts.get(e.relation_type, 0) + 1
        cnum, dtitle = clause_map.get(e.evidence_clause_id, (None, None))
        edge_outs.append(
            KgEdgeOut(
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
                review_status=e.review_status,
                usable_as_legal_basis=(e.review_status == "approved"),
            )
        )

    return GraphPayloadOut(
        country_code=cc,
        node_counts_by_type=node_counts,
        edge_counts_by_relation=edge_counts,
        nodes=[KgNodeOut.model_validate(n) for n in nodes],
        edges=edge_outs,
    )


# ==================== 管理端与律所复核端接口 ====================


@admin_router.post("/extract")
async def trigger_country_extraction(
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str = Query(default="SG", min_length=2, max_length=2),
    translate_sample_per_doc: int = Query(default=6, ge=1, le=50),
) -> dict:
    """一键初始化术语库/解读要点,并对指定国家执行六要素+十二关系知识图谱抽取与核心条款翻译。"""
    return await extract_knowledge_for_country(
        session, country_code.upper(), translate_sample_per_doc=translate_sample_per_doc
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
    _: AdminOrReviewerDep,
    session: SessionDep,
    country_code: str | None = None,
    review_status: str | None = None,
    document_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ClauseTranslationOut]:
    stmt = (
        select(ClauseTranslation, Clause, Document)
        .join(Clause, Clause.id == ClauseTranslation.clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .order_by(ClauseTranslation.updated_at.desc(), Clause.ordinal)
        .limit(limit)
    )
    if country_code:
        stmt = stmt.where(Document.country_code == country_code.upper())
    if review_status:
        stmt = stmt.where(ClauseTranslation.review_status == review_status)
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    rows = list((await session.execute(stmt)).all())
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
    tr = await translate_clause(session, clause, doc.country_code)
    await session.commit()
    outs = await _build_translation_outs(session, [(tr, clause, doc)])
    return outs[0]


@admin_router.post("/documents/{document_id}/translate")
async def trigger_document_translate(
    document_id: uuid.UUID,
    _: AdminOrReviewerDep,
    session: SessionDep,
    limit_clauses: int = Query(default=30, ge=1, le=200),
) -> dict:
    trs = await translate_document_latest_version(session, document_id, limit_clauses=limit_clauses)
    return {"document_id": str(document_id), "translated_count": len(trs)}


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
    if body.translated_text:
        tr.translated_text = body.translated_text.strip()
        passed, issues, checked_hits = verify_translation(
            clause.original_text, tr.translated_text, tr.glossary_hits or []
        )
        tr.quality_check_passed = passed
        tr.quality_issues = issues
        tr.glossary_hits = checked_hits
    tr.review_status = body.decision
    tr.reviewer_id = user.user_id
    tr.reviewer_note = body.reviewer_note
    tr.reviewed_at = datetime.now(UTC)
    tr.updated_at = datetime.now(UTC)
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
    edge.review_status = body.decision
    edge.reviewed_by = user.user_id
    edge.reviewed_at = datetime.now(UTC)
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
        review_status=edge.review_status,
        usable_as_legal_basis=(edge.review_status == "approved"),
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
                select(func.count(Document.id)).where(Document.country_code == cc)
            )
        ).scalar_one()
        clauses_count = (
            await session.execute(
                select(func.count(Clause.id))
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.country_code == cc)
            )
        ).scalar_one()
        tr_total = (
            await session.execute(
                select(func.count(ClauseTranslation.id))
                .join(Clause, Clause.id == ClauseTranslation.clause_id)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Document.country_code == cc)
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
                    ClauseTranslation.review_status == "approved",
                )
            )
        ).scalar_one()
        nodes_count = (
            await session.execute(select(func.count(KgNode.id)).where(KgNode.country_code == cc))
        ).scalar_one()
        edges_count = (
            await session.execute(select(func.count(KgEdge.id)).where(KgEdge.country_code == cc))
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
        .where(Document.country_code == country_code.upper())
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
    return await _build_graph_payload(session, country_code, only_approved=only_approved)


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
