"""Source recovery is distinct from releasing the old file or approving legal content."""

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.binding_status import source_binding_statuses
from app.ingestion.models import Document, DocumentVersion
from app.ingestion.safety import document_available, document_is_held
from app.knowledge.history import payload_hash, propose
from app.knowledge.models import KnowledgeAuditRecord

from .conftest import bearer, login
from .helpers import make_pdf
from .review_s1_s2_contracts import make_document


async def recovered_source(session, country="XY", identity="Privacy Act", edition="Edition 2024"):
    held, old, clauses = await make_document(session)
    held.country_code = country
    await propose(session, held, {"safety_hold": True, "reason": "original file mismatch"})
    doc = Document(
        site_id=held.site_id,
        country_code=country,
        title=identity,
        source_url="https://authority.invalid/" + str(uuid.uuid4()),
        language="en",
        document_type="LAW",
        relevance="relevant",
    )
    session.add(doc)
    await session.flush()
    archive = ArchiveStore(get_settings().archive_dir)
    raw = make_pdf([[identity, edition]])
    digest, path = archive.put(raw)
    version = DocumentVersion(
        document_id=doc.id,
        version_no=1,
        content_hash=digest,
        archive_path=path,
        mime_type="application/pdf",
        size_bytes=len(raw),
        final_url=doc.source_url,
        state="parsed",
    )
    session.add(version)
    await session.flush()
    payload = {
        "held_document_id": str(held.id),
        "canonical_document_id": str(doc.id),
        "canonical_version_id": str(version.id),
        "official_source_url": doc.source_url,
        "final_url": version.final_url,
        "content_hash": digest,
        "size_bytes": len(raw),
        "expected_identity": identity,
        "expected_edition": edition,
        "cover_evidence": identity + "\n" + edition,
        "old_binding_remains_held": True,
        "human_approval_inherited": False,
        "review_status": "pending_review",
    }
    record = KnowledgeAuditRecord(
        target_type="documents",
        target_id=held.id,
        action="source_binding_resolution",
        payload=payload,
        content_hash=payload_hash(payload),
    )
    session.add(record)
    await session.commit()
    return archive, held, old, clauses, doc, version, record


@pytest.mark.parametrize(
    "country,identity,edition",
    [("XY", "Privacy Act", "Edition 2024"), ("ZZ", "Loi sur les données", "Version française")],
)
async def test_replacement_status_is_generic_and_does_not_release_hold(
    session_factory, country, identity, edition
):
    async with session_factory() as s:
        archive, held, old, clauses, doc, version, record = await recovered_source(
            s, country, identity, edition
        )
        before = (old.content_hash, clauses[0].original_text, version.review_status, record.payload)
        rows = await source_binding_statuses(s, archive, country.lower())
        assert len(rows) == 1 and rows[0]["status"] == "replacement_available"
        assert rows[0]["replacement"]["document_id"] == str(doc.id)
        assert rows[0]["replacement"]["legal_status"] == "unknown"
        assert await document_is_held(s, held.id)
        assert not await s.scalar(
            select(Document.id).where(Document.id == held.id, document_available())
        )
        assert before == (
            old.content_hash,
            clauses[0].original_text,
            version.review_status,
            record.payload,
        )
        assert await source_binding_statuses(s, archive, "QQ") == []


@pytest.mark.parametrize(
    "failure",
    [
        "tampered_audit",
        "foreign_document",
        "held_replacement",
        "rejected",
        "missing_archive",
        "new_version",
    ],
)
async def test_invalid_or_outdated_replacement_keeps_unresolved_alert(session_factory, failure):
    async with session_factory() as s:
        archive, held, _, _, doc, version, record = await recovered_source(s)
        if failure == "tampered_audit":
            record.content_hash = "0" * 64
        elif failure == "foreign_document":
            doc.country_code = "ZZ"
        elif failure == "held_replacement":
            await propose(s, doc, {"safety_hold": True, "reason": "replacement mismatch"})
        elif failure == "rejected":
            version.review_status = "rejected"
        elif failure == "missing_archive":
            version.archive_path = "unavailable/" + version.content_hash
        elif failure == "new_version":
            s.add(
                DocumentVersion(
                    document_id=doc.id,
                    version_no=2,
                    content_hash="b" * 64,
                    archive_path="unavailable/new",
                    size_bytes=100,
                    mime_type="application/pdf",
                    final_url=doc.source_url,
                    state="downloaded",
                )
            )
        await s.commit()
        rows = await source_binding_statuses(s, archive, "XY")
        status = next(r for r in rows if r["document_id"] == str(held.id))
        assert status["status"] == "needs_source" and status["replacement"] is None
        assert await document_is_held(s, held.id)


async def test_graph_separates_open_issue_from_recovered_history(client, seed, session_factory):
    async with session_factory() as s:
        _, held, _, _, _, _, _ = await recovered_source(s)
        held_id = str(held.id)
    headers = bearer(await login(client, "admin", "admin@example.com"))
    response = await client.get("/api/admin/knowledge/graph?country_code=XY", headers=headers)
    assert response.status_code == 200
    q = response.json()["quality_summary"]
    assert q["source_integrity_held_documents"] == 1
    assert q["source_integrity_unresolved_documents"] == 0
    assert q["source_integrity_replaced_documents"] == 1
    assert q["source_integrity_records"][0]["document_id"] == held_id
    async with session_factory() as s:
        assert await document_is_held(s, uuid.UUID(held_id))
