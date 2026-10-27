import hashlib

from sqlalchemy import func, select

from app.analysis.models import ReportVersion
from app.analysis.tools import ToolBox
from app.cli import batch_translate
from app.ingestion.models import Clause
from app.ingestion.safety import document_is_held, report_held_citation_ids
from app.knowledge.history import propose
from app.knowledge.models import ClauseTranslation

from .conftest import bearer, login
from .review_s1_s2_contracts import make_document, make_edge


async def test_hold_preserves_sources_and_approvals_but_blocks_agent_and_translation(
    session_factory, monkeypatch
):
    from app.knowledge import translator

    async def forbidden(*args, **kwargs):
        raise AssertionError("held sources must not reach the model")

    monkeypatch.setattr(translator, "_call_llm_translate", forbidden)
    text = "An organisation must protect personal data under this Act and ensure legal safeguards."
    async with session_factory() as session:
        doc, _, clauses = await make_document(session, [text])
        edge, _, _ = await make_edge(session, clauses[0], "approved")
        session.add(
            ClauseTranslation(
                clause_id=clauses[0].id,
                translated_text="组织应依法保护个人数据。",
                engine="llm",
                model_name="test",
                review_status="approved",
            )
        )
        await session.commit()
        await propose(session, doc, {"safety_hold": True, "reason": "wrong archive binding"})
        await session.commit()
        assert await document_is_held(session, doc.id)
        assert await ToolBox(session, "SG")._load_candidates() == []
        assert await ToolBox(session, "SG").query_kg_obligations([str(clauses[0].id)]) == []
        assert edge.review_status == "approved" and clauses[0].original_text == text
        assert clauses[0].text_hash == hashlib.sha256(text.encode()).hexdigest()
        assert await session.scalar(select(func.count()).select_from(Clause)) == 1
    stats = await batch_translate("SG", sf=session_factory, dry_run=True)
    assert stats["total_eligible"] == 0


async def test_held_source_is_visible_to_admin_but_not_public_knowledge(
    client, seed, session_factory
):
    async with session_factory() as session:
        doc, _, clauses = await make_document(session)
        edge, source, _ = await make_edge(session, clauses[0], "approved")
        source.document_id = doc.id
        session.add(
            ClauseTranslation(
                clause_id=clauses[0].id,
                translated_text="组织应保护个人数据。",
                engine="llm",
                model_name="test",
                review_status="approved",
            )
        )
        await session.commit()
        await propose(session, doc, {"safety_hold": True, "reason": "wrong archive binding"})
        await session.commit()
        doc_id, clause_id, edge_id = str(doc.id), str(clauses[0].id), str(edge.id)
    admin = bearer(await login(client, "admin", "admin@example.com"))
    user = bearer(await login(client, "user", "a-member@example.com"))
    docs = (await client.get("/api/admin/documents", headers=admin)).json()
    assert docs[0]["safety_hold"] is True
    assert (await client.get(f"/api/admin/documents/{doc_id}", headers=admin)).status_code == 200
    assert (
        await client.post(f"/api/admin/knowledge/documents/{doc_id}/translate", headers=admin)
    ).status_code == 409
    assert (
        await client.get(f"/api/knowledge/regulations/{doc_id}", headers=user)
    ).status_code == 409
    assert (
        await client.get(f"/api/analysis/clauses/{clause_id}/citation-card", headers=user)
    ).status_code == 409
    overview = (await client.get("/api/knowledge/overview", headers=user)).json()
    country = next(row for row in overview if row["country_code"] == "SG")
    assert country["clauses_count"] == country["translations_count"] == 0
    assert country["approved_translations_count"] == country["kg_edges_count"] == 0
    topics = (await client.get("/api/knowledge/topics?country_code=SG", headers=user)).json()
    assert not any(item["document_id"] == doc_id for item in topics["items"])
    assert (
        await client.post(
            f"/api/admin/knowledge/edges/{edge_id}/review",
            headers=admin,
            json={"decision": "approved"},
        )
    ).status_code == 409


async def test_report_hold_checks_nested_actual_citation_ids(session_factory):
    async with session_factory() as session:
        doc, _, clauses = await make_document(session)
        await session.commit()
        await propose(session, doc, {"safety_hold": True, "reason": "wrong archive binding"})
        await session.commit()
        report = ReportVersion(dimensions_json=[{"citations": [{"clause_id": str(clauses[0].id)}]}])
        assert await report_held_citation_ids(session, report) == [clauses[0].id]
