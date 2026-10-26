"""Read-only handling status for held sources; never release a hold or approve law."""

import uuid

from sqlalchemy import select

from app.ingestion.archive import ArchiveIntegrityError
from app.ingestion.bindings import normalized_identity
from app.ingestion.models import Document, DocumentVersion
from app.ingestion.safety import document_available, document_is_held, holds_for_documents
from app.knowledge.history import payload_hash
from app.knowledge.models import KnowledgeAuditRecord
from app.updates.source_state import current_source_query


async def _replacement(session, archive, held, record, current):
    p = record.payload
    if not isinstance(p, dict) or record.content_hash != payload_hash(p):
        return None
    try:
        doc_id = uuid.UUID(p["canonical_document_id"])
        version_id = uuid.UUID(p["canonical_version_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    if (
        p.get("held_document_id") != str(held.id)
        or p.get("old_binding_remains_held") is not True
        or p.get("human_approval_inherited") is not False
        or doc_id == held.id
        or current.get(doc_id) != version_id
    ):
        return None
    doc = await session.get(Document, doc_id)
    version = await session.get(DocumentVersion, version_id)
    if (
        not doc
        or not version
        or version.document_id != doc.id
        or doc.country_code != held.country_code
        or doc.relevance not in {"relevant", "related"}
        or version.state != "parsed"
        or version.review_status == "rejected"
        or version.legal_status == "repealed"
        or await document_is_held(session, doc.id)
        or p.get("official_source_url") != doc.source_url
        or p.get("final_url") != version.final_url
        or p.get("content_hash") != version.content_hash
        or p.get("size_bytes") != version.size_bytes
    ):
        return None
    cover = p.get("cover_evidence")
    expected = (p.get("expected_identity"), p.get("expected_edition"))
    if not isinstance(cover, str) or any(
        not isinstance(value, str)
        or not value.strip()
        or normalized_identity(value) not in normalized_identity(cover)
        for value in expected
    ):
        return None
    try:
        raw = archive.read(version.archive_path)
    except (OSError, ArchiveIntegrityError):
        return None
    if len(raw) != version.size_bytes or archive.sha256(raw) != version.content_hash:
        return None
    return {
        "document_id": str(doc.id),
        "title": doc.title,
        "version_id": str(version.id),
        "edition": p["expected_edition"],
        "source_url": doc.source_url,
        "review_status": version.review_status,
        "legal_status": version.legal_status,
    }


async def source_binding_statuses(session, archive, country_code):
    held_docs = list(
        await session.scalars(
            select(Document)
            .where(Document.country_code == country_code.upper(), ~document_available())
            .order_by(Document.title, Document.id)
        )
    )
    if not held_docs:
        return []
    holds = await holds_for_documents(session, [doc.id for doc in held_docs])
    records = list(
        await session.scalars(
            select(KnowledgeAuditRecord)
            .where(
                KnowledgeAuditRecord.target_type == "documents",
                KnowledgeAuditRecord.target_id.in_([doc.id for doc in held_docs]),
                KnowledgeAuditRecord.action == "source_binding_resolution",
            )
            .order_by(KnowledgeAuditRecord.created_at.desc(), KnowledgeAuditRecord.id.desc())
        )
    )
    current = dict((await session.execute(current_source_query())).all())
    result = []
    for doc in held_docs:
        replacement = None
        for record in records:
            if record.target_id == doc.id:
                replacement = await _replacement(session, archive, doc, record, current)
                if replacement:
                    break
        result.append(
            {
                "document_id": str(doc.id),
                "title": doc.title,
                "country_code": doc.country_code,
                "reason": holds.get(doc.id, {}).get("reason"),
                "status": "replacement_available" if replacement else "needs_source",
                "replacement": replacement,
                "old_source_remains_held": True,
            }
        )
    return result
