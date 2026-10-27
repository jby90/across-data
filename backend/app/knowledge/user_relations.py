"""Read-only, document/version-scoped relations for regulation readers.

Evidence eligibility is shared with analysis/admin; this is a projection, not a
second graph or an extraction pass. Disconnected clause relationships remain visible.
"""

import uuid

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_available, document_is_held
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.models import KgEdge, KgNode

RELATION_LABELS = {
    "PUBLISHES": "发布",
    "AMENDS": "修订",
    "REPEALS": "废止",
    "GOVERNS": "适用于",
    "PROHIBITS": "禁止",
    "REQUIRES": "要求",
    "EXEMPTS": "豁免",
    "SUPERVISES": "监管",
    "PENALIZES": "处罚",
    "APPLIES_TO_SCENARIO": "适用场景",
    "RESTRICTS_CROSS_BORDER": "限制跨境传输",
    "REQUIRES_ADMISSION": "要求准入申请",
}


class ReaderNode(BaseModel):
    id: uuid.UUID
    name: str
    original_name: str | None
    type: str


class ReaderRelation(BaseModel):
    id: uuid.UUID
    source: uuid.UUID
    target: uuid.UUID
    relation_type: str
    label: str
    clause_id: uuid.UUID
    clause_number: str | None
    quote: str
    review_status: str


class ReaderRelations(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    version_no: int | None
    source_url: str
    total: int
    offset: int
    limit: int
    nodes: list[ReaderNode]
    relations: list[ReaderRelation]


async def reader_relations(session, document_id, version_id, offset, limit):
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "法规文件不存在")
    if await document_is_held(session, doc.id):
        raise HTTPException(409, "该文件的来源关联正在核查，请选择其他法规")
    version_query = select(DocumentVersion).where(
        DocumentVersion.document_id == doc.id, DocumentVersion.state == "parsed"
    )
    if version_id:
        version_query = version_query.where(DocumentVersion.id == version_id)
    version = await session.scalar(version_query.order_by(DocumentVersion.version_no.desc()))
    if version_id and not version:
        raise HTTPException(404, "归档版本不存在或尚未完成解析")
    result = ReaderRelations(
        document_id=doc.id,
        version_id=version.id if version else None,
        version_no=version.version_no if version else None,
        source_url=(version.final_url or doc.source_url) if version else doc.source_url,
        total=0,
        offset=offset,
        limit=limit,
        nodes=[],
        relations=[],
    )
    if not version:
        return result
    src, dst = aliased(KgNode), aliased(KgNode)
    available_docs = select(Document.id).where(document_available())
    query = (
        select(KgEdge, Clause, src, dst)
        .join(Clause, Clause.id == KgEdge.evidence_clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(src, src.id == KgEdge.source_node_id)
        .join(dst, dst.id == KgEdge.target_node_id)
        .where(
            Clause.version_id == version.id,
            Document.id == doc.id,
            DocumentVersion.legal_status != "repealed",
            *active_edge_filters(),
            KgEdge.is_structural.is_(False),
            src.review_status.in_(("approved", "pending_review")),
            dst.review_status.in_(("approved", "pending_review")),
            or_(src.document_id.is_(None), src.document_id.in_(available_docs)),
            or_(dst.document_id.is_(None), dst.document_id.in_(available_docs)),
        )
    )
    result.total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (
        await session.execute(
            query.order_by(Clause.ordinal, Clause.id, KgEdge.id).offset(offset).limit(limit)
        )
    ).all()
    nodes = {}
    for edge, clause, source, target in rows:
        for node in (source, target):
            nodes[node.id] = ReaderNode(
                id=node.id,
                name=node.name_zh,
                original_name=node.name_orig,
                type=node.node_type,
            )
        result.relations.append(
            ReaderRelation(
                id=edge.id,
                source=edge.source_node_id,
                target=edge.target_node_id,
                relation_type=edge.relation_type,
                label=RELATION_LABELS.get(edge.relation_type, edge.relation_type),
                clause_id=clause.id,
                clause_number=clause.clause_number,
                quote=edge.evidence_quote,
                review_status=edge.review_status,
            )
        )
    result.nodes = list(nodes.values())
    return result
