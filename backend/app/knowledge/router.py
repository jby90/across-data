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
    """按《需求文档 1.3.3》生成 8 类核心法规/要素结构化属性画像。"""
    cc = country_code.upper()
    t_low = (title or "").lower()
    rels = rel_types or set()
    is_av = (
        "autonomous" in t_low
        or "road traffic" in t_low
        or "trial" in t_low
        or "REQUIRES_ADMISSION" in rels
    )
    is_iot = "iot" in t_low or "انترنت" in t_low

    if is_av:
        industry = "智能网联汽车 / 自动驾驶公共道路测试与准入 (L3-L5)"
    elif is_iot:
        industry = "车联网远程信息处理 (Telematics) / 车载 eSIM 与物联网终端"
    else:
        industry = "汽车出海与跨行业通用（覆盖车主、驾乘人员及车联网用户个人数据）"

    scope_str = (
        "、".join(sorted(set(data_names)))
        if data_names
        else (
            "自动驾驶传感器与车载黑匣子记录数据、个人数据(含车主与驾乘识别数据)"
            if is_av
            else "个人数据(含车主身份、VIN关联轨迹、座舱生物识别及受托处理车联网数据)"
        )
    )

    if cc == "SG":
        transfer_cond = (
            "须满足 PDPA 第 26 条同等保护标准 (Comparable Protection)：签署具法律约束力的跨境合同"
            "/BCR/认证，并履行出境告知同意；自动驾驶测试数据需遵守 LTA 路测许可附加条件"
        )
        retention = (
            "自动驾驶黑匣子(Data Recorder)数据法定至少留存 3 年 (AV Rules r9)；"
            "一般个人数据在收集目的达成且无商业/法律必要时须立即停止留存或匿名化 (PDPA s25)"
        )
        auth_req = (
            "收集/使用/披露前须履行通知并取得个人同意 (PDPA s13-20)；"
            "开展自动驾驶公共道路试验须事先取得 LTA 书面批准 (Trial Approval)"
            if is_av
            else (
                "收集/使用/披露及出境前须履行目的告知并取得有效同意 (PDPA s13-20)，"
                "受托处理须签书面合约"
            )
        )
        penalty_amt = (
            "PDPC 行政罚款最高 100 万新元或新加坡年营业额 10%（以较高者为准，PDPA s48J）；"
            "违反自动驾驶路测规定可处最高 1 万新元罚款或监禁及吊销路测许可"
        )
        violation_lvl = "高风险（行政高额罚款 / 强制停止数据传输令 / 吊销自动驾驶路测准入许可）"
        admission_list = (
            "1. LTA 自动驾驶车辆道路测试申请表 "
            "(Application for Trial of Autonomous Motor Vehicles)；"
            "2. 车辆安全技术评估与测试区域方案；3. 安全员(Safety Operator)培训资质证明；"
            "4. 车载数据记录器(黑匣子)技术规范与 3 年留存承诺；5. 第三方责任保险证明；"
            "6. 跨境数据传输保护协议 (DPA/TIA)"
        )
    elif cc == "EG":
        transfer_cond = (
            "须取得埃及个人数据保护中心 (PDPC) 跨境传输许可证/授权"
            "（埃及第 151/2020 号法第 14 条），"
            "且接收国须具备不低于埃及的保护水平；车联网 IoT 平台须符合 NTRA 本地化连接要求"
        )
        retention = "仅在法定收集目的存续期间留存，期满或撤回同意后须销毁并登记台账"
        auth_req = (
            "处理与跨境传输须取得数据主体明示同意，并向埃及监管机构申请处理/出境许可及指定 DPO"
        )
        penalty_amt = (
            "违反第 151/2020 号法可处 10 万至 500 万埃及镑罚金，情节严重并处监禁及吊销许可证"
        )
        violation_lvl = "高风险（刑事罚金 + 行政吊销许可 + NTRA 车联网入网限制）"
        admission_list = (
            "1. 埃及 NTRA 车载通信终端与 eSIM 型号核准申请；"
            "2. 埃及 PDPC 个人数据处理及跨境传输许可证申请表；"
            "3. 数据保护官 (DPO) 备案材料；4. 车联网数据安全与架构说明"
        )
    elif cc == "TN":
        transfer_cond = (
            "跨境传输须事先获得突尼斯国家个人数据保护局 (INPDP) 授权"
            "（2004-63 号组织法第 50-52 条），并确保目的国提供充分保护"
        )
        retention = "数据处理目的实现后须销毁或归档匿名化，不得超期保留车主与行程记录"
        auth_req = "须向 INPDP 提交事前申报或申请授权，并取得数据主体书面/明确同意"
        penalty_amt = (
            "未经授权跨境传输或违规处理可处 1,000 至 50,000 突尼斯第纳尔罚金及 1 至 5 年监禁"
        )
        violation_lvl = "中高风险（INPDP 事前审批否决 / 刑事罚金与监禁）"
        admission_list = (
            "1. 突尼斯 INPDP 个人数据处理事前申报单；2. 个人数据跨境传输专项授权申请表；"
            "3. 境外接收方数据保护保障协议"
        )
    elif cc == "MA":
        transfer_cond = (
            "向境外传输个人数据须事先向摩洛哥国家数据保护委员会 (CNDP) 履行申报或获取授权"
            "（第 09-08 号法律第 43-44 条）"
        )
        retention = "须按向 CNDP 申报的留存期限执行，到期自动清除或匿名化"
        auth_req = "数据处理须向 CNDP 提交事前申报/授权申请，并取得数据主体明确同意"
        penalty_amt = (
            "违规处理或未经授权出境可处 10,000 至 300,000 摩洛哥迪拉姆罚金及 3 个月至 2 年监禁"
        )
        violation_lvl = "中高风险（CNDP 行政查处 / 撤销授权 / 刑事罚金）"
        admission_list = (
            "1. 摩洛哥 CNDP 数据处理申报/授权表 (Formulaire de déclaration/autorisation)；"
            "2. 跨境数据传输申报附件 (Transfert de données à l'étranger)；"
            "3. 车联网隐私告知文本(法/阿双语)"
        )
    else:
        transfer_cond = (
            "须符合黎巴嫩第 81/2018 号《电子交易与个人数据法》告知、同意及经济贸易部备案要求"
        )
        retention = "仅限合法收集目的所需期限，须提供车主查阅、更正与删除渠道"
        auth_req = "收集处理前须明确告知处理目的、接收方及权利，并向黎巴嫩主管机关履行登记/许可手续"
        penalty_amt = "违反第 81/2018 号法可处法定罚金及最高 3 年监禁"
        violation_lvl = "中风险（行政处罚与司法追责）"
        admission_list = (
            "1. 黎巴嫩第 81/2018 号法项下数据处理登记/许可申报材料；2. 车载数据隐私告知与同意书；"
            "3. 跨境传输安全保障说明"
        )

    if obl_names:
        auth_req = f"{auth_req}（关联义务：{'、'.join(sorted(set(obl_names))[:3])}）"

    return {
        "applicable_industry": industry,
        "data_scope": scope_str,
        "cross_border_condition": transfer_cond,
        "retention_period": retention,
        "authorization_requirement": auth_req,
        "penalty_amount": penalty_amt,
        "violation_level": violation_lvl,
        "admission_checklist": admission_list,
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
            KgEdge.review_status != "superseded",
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
    if clause_ids:
        c_rows = (
            await session.execute(
                select(Clause.id, Clause.clause_number, Document.title, Clause.original_text)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Clause.id.in_(clause_ids))
            )
        ).all()
        for cid, cnum, dtitle, orig_txt in c_rows:
            clause_map[cid] = (cnum, dtitle, orig_txt or "")

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
                usable_as_legal_basis=(e.review_status == "approved"),
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
        1
        for e in edge_outs
        if not e.is_structural and e.judge_verdict not in ("yes", "partial")
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
        merged_attrs = {**(n.attributes or {}), **eight_attrs}
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

    return GraphPayloadOut(
        country_code=cc,
        node_counts_by_type=node_counts,
        edge_counts_by_relation=edge_counts,
        quality_summary={
            "documents_total": docs_total,
            "documents_core": doc_scope.get("relevant", 0),
            "documents_related": doc_scope.get("related", 0),
            "documents_out_of_scope": docs_out_of_scope,
            "documents_extracted": docs_total - docs_out_of_scope,
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
    else:
        stmt = stmt.where(
            ClauseTranslation.review_status != TranslationReviewStatus.SUPERSEDED.value
        )
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
        confidence=float(getattr(edge, "confidence", 0.85)),
        judge_verdict=str(getattr(edge, "judge_verdict", "yes")),
        judge_reason=getattr(edge, "judge_reason", None),
        is_structural=bool(getattr(edge, "is_structural", False)),
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
                .where(
                    Document.country_code == cc,
                    ClauseTranslation.review_status.notin_(
                        ("superseded", "not_translated", "translation_failed")
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
                    ClauseTranslation.review_status == "approved",
                )
            )
        ).scalar_one()
        nodes_count = (
            await session.execute(
                select(func.count(KgNode.id)).where(
                    KgNode.country_code == cc,
                    KgNode.review_status != "superseded",
                )
            )
        ).scalar_one()
        edges_count = (
            await session.execute(
                select(func.count(KgEdge.id)).where(
                    KgEdge.country_code == cc,
                    KgEdge.review_status != "superseded",
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
            ClauseTranslation.review_status.notin_(
                ("superseded", "not_translated", "translation_failed")
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
                        ClauseTranslation.review_status.notin_(
                            ("superseded", "not_translated", "translation_failed")
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
                        KgEdge.review_status != "superseded",
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
    stmt = select(Document).where(Document.country_code == cc).order_by(Document.title)
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
                "现行有效归档版本"
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
                            KgEdge.review_status != "superseded",
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
