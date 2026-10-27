"""Project validated graph evidence into user-facing compliance topic cards."""

from sqlalchemy import select

from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.models import KgEdge, KgNode

TOPICS = {
    "RESTRICTS_CROSS_BORDER": ("transfers", "跨境传输"),
    "REQUIRES_ADMISSION": ("admission", "准入与申报"),
    "PENALIZES": ("penalties", "责任与罚则"),
    "APPLIES_TO_SCENARIO": ("scope", "适用范围"),
    "GOVERNS": ("scope", "适用范围"),
    "REQUIRES": ("obligations", "合规义务"),
    "PROHIBITS": ("restrictions", "限制与禁止"),
    "EXEMPTS": ("exemptions", "例外与豁免"),
    "SUPERVISES": ("regulators", "监管要求"),
}


async def topic_cards(session, country_code, document_id=None, clause_id=None):
    stmt = (
        select(KgEdge, Clause, DocumentVersion, Document, KgNode)
        .join(Clause, Clause.id == KgEdge.evidence_clause_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KgNode, KgNode.id == KgEdge.target_node_id)
        .where(
            *active_edge_filters(),
            KgEdge.is_structural.is_(False),
            Document.country_code == country_code.upper(),
            DocumentVersion.legal_status != "repealed",
            KgNode.review_status.in_(("approved", "pending_review")),
        )
        .order_by(Document.title, Clause.ordinal, KgEdge.id)
    )
    if document_id:
        stmt = stmt.where(Document.id == document_id)
    if clause_id:
        stmt = stmt.where(Clause.id == clause_id)
    cards = []
    for edge, clause, version, doc, target in (await session.execute(stmt)).all():
        topic, label = TOPICS.get(edge.relation_type, ("other", "其他要求"))
        cards.append(
            dict(
                id=str(edge.id),
                topic=topic,
                topic_label=label,
                title=target.name_zh,
                document_id=str(doc.id),
                document_title=doc.title,
                clause_id=str(clause.id),
                clause_number=clause.clause_number or str(clause.ordinal),
                version_no=version.version_no,
                source_url=doc.source_url,
                quote=edge.evidence_quote,
                source_label="原文关联要点",
                review_status=edge.review_status,
            )
        )
    return cards
