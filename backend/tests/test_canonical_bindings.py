import pytest
from sqlalchemy import func, select

from app.ingestion.archive import ArchiveStore
from app.ingestion.bindings import binding_resolutions, record_canonical_binding
from app.ingestion.models import Document, DocumentVersion
from app.ingestion.safety import document_is_held
from app.knowledge.history import propose
from app.knowledge.models import KnowledgeAuditRecord

from .helpers import make_pdf
from .review_s1_s2_contracts import make_document, make_edge


async def test_binding_resolution_is_append_only_identity_checked_and_not_approval(
    session_factory,
    tmp_path,
):
    async with session_factory() as session:
        held, old_version, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0], "approved")
        await propose(session, held, {"safety_hold": True, "reason": "wrong file"})
        replacement = Document(
            site_id=held.site_id,
            country_code="SG",
            title=held.title,
            source_url="https://assets.egazette.gov.sg/test.pdf",
            language="en",
            document_type="LAW",
        )
        session.add(replacement)
        await session.flush()
        archive = ArchiveStore(tmp_path)
        raw = make_pdf([["PERSONAL DATA PROTECTION ACT 2012", "2020 REVISED EDITION"]])
        digest, path = archive.put(raw)
        version = DocumentVersion(
            document_id=replacement.id,
            version_no=1,
            content_hash=digest,
            archive_path=path,
            size_bytes=len(raw),
            mime_type="application/pdf",
            final_url=replacement.source_url,
            state="parsed",
        )
        session.add(version)
        await session.flush()
        before = (
            old_version.content_hash,
            clauses[0].original_text,
            edge.evidence_quote,
            edge.review_status,
            edge.reviewed_at,
        )
        args = dict(
            expected_identity="PERSONAL DATA PROTECTION ACT 2012",
            expected_edition="2020 REVISED EDITION",
            acquisition_note="official archive, review pending",
        )
        with pytest.raises(ValueError, match="cover"):
            await record_canonical_binding(
                session,
                archive,
                held.id,
                version.id,
                **(args | {"expected_edition": "2024 REVISED EDITION"}),
            )
        resolution = await record_canonical_binding(session, archive, held.id, version.id, **args)
        again = await record_canonical_binding(session, archive, held.id, version.id, **args)
        await session.commit()
        assert resolution.id == again.id
        assert await session.scalar(select(func.count()).select_from(KnowledgeAuditRecord)) == 1
        assert before == (
            old_version.content_hash,
            clauses[0].original_text,
            edge.evidence_quote,
            edge.review_status,
            edge.reviewed_at,
        )
        assert await document_is_held(session, held.id)
        assert version.review_status == "pending_review" and version.legal_status == "unknown"
        assert (await binding_resolutions(session, held.id))[0]["human_approval_inherited"] is False
