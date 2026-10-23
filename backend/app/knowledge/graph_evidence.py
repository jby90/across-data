"""Shared active-edge eligibility and evidence-backed display attributes."""

from sqlalchemy import func, or_, select

from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import KgEdge


def active_edge_filters():
    latest = (
        select(func.max(DocumentVersion.version_no))
        .where(
            DocumentVersion.document_id == Document.id,
            DocumentVersion.state == "parsed",
        )
        .correlate(Document)
        .scalar_subquery()
    )
    current_source = (
        select(Clause.id)
        .join(DocumentVersion, Clause.version_id == DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            Clause.id == KgEdge.evidence_clause_id,
            DocumentVersion.version_no == latest,
            DocumentVersion.state == "parsed",
            Document.relevance.in_(("relevant", "related")),
            KgEdge.evidence_quote != "",
            func.strpos(Clause.original_text, KgEdge.evidence_quote) > 0,
        )
        .correlate(KgEdge)
        .exists()
    )
    return (
        KgEdge.review_status.in_(("approved", "pending_review")),
        KgEdge.extractor_model != "kg-rule-extractor-v1",
        or_(KgEdge.is_structural.is_(True), KgEdge.judge_verdict == "yes"),
        or_(KgEdge.is_structural.is_(True), current_source),
    )


def evidence_attributes(edges, node_by_id, clause_map, *, node_id=None):
    """Only show actual current target labels; no guessed amounts, periods or legal conclusions.

    clause_map is id -> (number, document title, original). Callers must fetch actual source text.
    Returned provenance is suitable for a reviewer to navigate back to the original.
    """
    values, provenance = {}, {}
    dimensions = {
        "RESTRICTS_CROSS_BORDER": "cross_border_condition",
        "REQUIRES_ADMISSION": "admission_checklist",
        "PENALIZES": "penalty_amount",
        "APPLIES_TO_SCENARIO": "applicable_industry",
    }
    for edge in edges:
        if edge.is_structural or edge.judge_verdict != "yes":
            continue
        if edge.review_status not in ("approved", "pending_review"):
            continue
        if node_id is not None and node_id not in (edge.source_node_id, edge.target_node_id):
            continue
        target = node_by_id.get(edge.target_node_id)
        source = node_by_id.get(edge.source_node_id)
        clause = clause_map.get(edge.evidence_clause_id)
        if (
            not target
            or not source
            or not clause
            or not edge.evidence_quote
            or edge.evidence_quote not in clause[2]
        ):
            continue
        keys = []
        if target.node_type == "DATA_CATEGORY":
            keys.append("data_scope")
        if edge.relation_type in dimensions:
            keys.append(dimensions[edge.relation_type])
        if edge.relation_type == "REQUIRES_ADMISSION":
            keys.append("authorization_requirement")
        if edge.relation_type == "REQUIRES" and any(
            word in target.name_zh for word in ("留存", "保留期限")
        ):
            keys.append("retention_period")
        for key in keys:
            label = "已复核关联：" if edge.review_status == "approved" else "待人工复核关联："
            values.setdefault(key, []).append(label + target.name_zh)
            provenance.setdefault(key, []).append(
                {
                    "edge_id": str(edge.id),
                    "clause_id": str(edge.evidence_clause_id),
                    "clause_number": clause[0],
                    "document_title": clause[1],
                    "evidence_quote": edge.evidence_quote,
                    "review_status": edge.review_status,
                }
            )
    return {key: "；".join(dict.fromkeys(texts)) for key, texts in values.items()}, provenance
