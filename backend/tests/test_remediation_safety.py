"""Data safety regressions use temporary PostgreSQL/archive, never development records."""

from sqlalchemy import func, select

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, PipelineEvent
from app.ingestion.pipeline import parse_version
from app.knowledge import extractor
from app.knowledge.models import KnowledgeAuditRecord, KnowledgeChangeProposal, RelationType

from .review_s1_s2_contracts import make_document, make_edge


async def test_reparse_preserves_existing_clause_and_approval(session_factory, tmp_path):
    async with session_factory() as session:
        _, version, clauses = await make_document(session)
        archive = ArchiveStore(tmp_path)
        version.content_hash, version.archive_path = archive.put(b"original source archive")
        edge, _, _ = await make_edge(session, clauses[0], "approved")
        await session.commit()
        before = (clauses[0].original_text, clauses[0].text_hash, edge.evidence_quote)
        await parse_version(session, archive, version, "en")
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Clause)) == 1
        assert before == (clauses[0].original_text, clauses[0].text_hash, edge.evidence_quote)
        event = await session.scalar(select(PipelineEvent))
        assert "preserved_existing_clauses" in event.message


async def test_changed_approved_edge_becomes_deduplicated_proposal(session_factory):
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, source, target = await make_edge(session, clauses[0], "approved")
        await session.commit()
        quote = edge.evidence_quote
        for _ in range(2):
            result = await extractor._upsert_edge(
                session,
                country_code="SG",
                source_node=source,
                target_node=target,
                relation_type=RelationType.REQUIRES,
                evidence_clause=clauses[0],
                evidence_quote="An organisation must notify the Commission of a breach.",
                extractor_model="new-model",
                judge_verdict="yes",
            )
            await session.flush()
            assert result.id == edge.id
            assert edge.review_status == "approved" and edge.evidence_quote == quote
        assert await session.scalar(select(func.count()).select_from(KnowledgeChangeProposal)) == 1


async def test_audit_snapshot_survives_mutation(session_factory):
    from app.knowledge.history import snapshot

    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0])
        snapshot(session, edge, "before_human_review")
        edge.review_status = "approved"
        snapshot(session, edge, "after_human_review")
        await session.commit()
        records = (await session.scalars(select(KnowledgeAuditRecord))).all()
        assert {row.payload["review_status"] for row in records} == {"pending_review", "approved"}
