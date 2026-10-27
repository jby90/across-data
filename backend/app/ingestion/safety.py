"""Source integrity holds are append-only proposals, never edits to source/approval records."""

from sqlalchemy import select

from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import KnowledgeChangeProposal


def document_available(document_id=Document.id):
    return (
        ~select(KnowledgeChangeProposal.id)
        .where(
            KnowledgeChangeProposal.target_type == "documents",
            KnowledgeChangeProposal.target_id == document_id,
            KnowledgeChangeProposal.status.in_(("pending_review", "approved")),
            KnowledgeChangeProposal.payload["safety_hold"].as_boolean().is_(True),
        )
        .correlate(Document)
        .exists()
    )


async def document_is_held(session, document_id):
    return bool(
        await session.scalar(
            select(KnowledgeChangeProposal.id)
            .where(
                KnowledgeChangeProposal.target_type == "documents",
                KnowledgeChangeProposal.target_id == document_id,
                KnowledgeChangeProposal.status.in_(("pending_review", "approved")),
                KnowledgeChangeProposal.payload["safety_hold"].as_boolean().is_(True),
            )
            .limit(1)
        )
    )


async def holds_for_documents(session, document_ids):
    rows = (
        (
            await session.scalars(
                select(KnowledgeChangeProposal).where(
                    KnowledgeChangeProposal.target_type == "documents",
                    KnowledgeChangeProposal.target_id.in_(document_ids),
                    KnowledgeChangeProposal.status.in_(("pending_review", "approved")),
                    KnowledgeChangeProposal.payload["safety_hold"].as_boolean().is_(True),
                )
            )
        ).all()
        if document_ids
        else []
    )
    return {row.target_id: row.payload for row in rows}


async def held_citation_ids(session, clause_ids):
    if not clause_ids:
        return []
    from app.ingestion.layout_safety import layout_held_clause_ids

    source_held = list(
        (
            await session.scalars(
                select(Clause.id)
                .join(DocumentVersion, Clause.version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(Clause.id.in_(clause_ids), ~document_available())
            )
        ).all()
    )
    return sorted(
        set(source_held) | set(await layout_held_clause_ids(session, clause_ids)), key=str
    )


async def report_held_citation_ids(session, report):
    import json
    import re
    import uuid

    raw = json.dumps(report.dimensions_json or [], ensure_ascii=False)
    ids = {
        uuid.UUID(value)
        for value in re.findall(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", raw)
    }
    return await held_citation_ids(session, ids)
