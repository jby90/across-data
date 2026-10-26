import copy

from sqlalchemy import select

from app.analysis.models import AnalysisTask, ReportVersion
from app.analysis.tools import ToolBox
from app.ingestion.layout_safety import layout_held_clause_ids
from app.ingestion.models import Clause, DocumentVersion
from app.ingestion.safety import report_held_citation_ids
from app.knowledge import extractor
from app.knowledge.graph_evidence import active_edge_filters
from app.knowledge.history import propose, record_payload
from app.knowledge.models import ClauseTranslation, KgEdge

from .conftest import bearer, login
from .review_s1_s2_contracts import make_document, make_edge


async def page_proposal(session, version, clause, page=1):
    return await propose(
        session,
        clause,
        {
            "source_parse_revision": dict(
                scope="whole_source_page",
                version_id=str(version.id),
                page_number=page,
                review_status="pending_review",
                active=False,
                usable_as_legal_basis=False,
            )
        },
    )


async def test_pending_page_repair_blocks_siblings_without_changing_approved_evidence(
    session_factory,
):
    async with session_factory() as s:
        _, version, clauses = await make_document(
            s,
            [
                "An organisation must protect personal data.",
                "A second fragment from the same damaged page.",
                "A different page must remain available to the analysis.",
            ],
        )
        for c, page in zip(clauses, [1, 1, 2], strict=True):
            c.page_start = c.page_end = page
        edge, _, _ = await make_edge(s, clauses[1], "approved")
        tr = ClauseTranslation(
            clause_id=clauses[1].id,
            translated_text="旧人工译文",
            engine="llm",
            model_name="test",
            review_status="approved",
            quality_check_passed=True,
        )
        s.add(tr)
        await s.commit()
        before = [record_payload(row) for row in (clauses[1], edge, tr)]
        await page_proposal(s, version, clauses[0])
        await s.commit()
        assert set(await layout_held_clause_ids(s, [c.id for c in clauses])) == {
            c.id for c in clauses[:2]
        }
        assert [c.clause_id for c in await ToolBox(s, "SG")._load_candidates()] == [
            str(clauses[2].id)
        ]
        assert not (await s.scalars(select(KgEdge).where(*active_edge_filters()))).all()
        assert await ToolBox(s, "SG").query_kg_obligations([str(clauses[1].id)]) == []
        report = ReportVersion(dimensions_json=[{"citations": [{"clause_id": str(clauses[1].id)}]}])
        assert await report_held_citation_ids(s, report) == [clauses[1].id]
        for row, expected in zip((clauses[1], edge, tr), before, strict=True):
            await s.refresh(row)
            assert record_payload(row) == expected


async def test_country_rebuild_does_not_reuse_old_layout_caches(session_factory, monkeypatch):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs["clause_text"])
        assert kwargs["clause_text"] == "Unaffected provision must protect personal data."
        return extractor.ClauseExtractionPayload(entities=[], relations=[]), "test-model"

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", fake)
    async with session_factory() as s:
        _, version, clauses = await make_document(
            s,
            [
                "Misparsed fee row.",
                "Another part of the same page.",
                "Unaffected provision must protect personal data.",
            ],
        )
        for c, page in zip(clauses, [1, 1, 2], strict=True):
            c.page_start = c.page_end = page
        await page_proposal(s, version, clauses[0])
        await s.commit()
        result = await extractor.extract_knowledge_for_country(s, "SG", translate_sample_per_doc=0)
        assert result["status"] == "completed" and result["sampled"] is False
        assert result["attempted_clauses"] == 1 and result["layout_held_clauses"] == 2
        assert result["layout_revisions_auto_activated"] is False and len(calls) == 1


async def test_all_latest_clauses_blocked_does_not_fall_back_to_old_version(session_factory):
    async with session_factory() as s:
        _, old_version, clauses = await make_document(s)
        version = DocumentVersion(
            document_id=old_version.document_id,
            version_no=2,
            content_hash="b" * 64,
            archive_path="bb/frozen",
            mime_type="application/pdf",
            size_bytes=100,
            state="parsed",
            final_url="https://example.org",
        )
        s.add(version)
        await s.flush()
        clause = Clause(
            version_id=version.id,
            ordinal=1,
            original_text="New parse under review.",
            text_hash="c" * 64,
            language="en",
            page_start=1,
            page_end=1,
        )
        s.add(clause)
        await s.flush()
        await page_proposal(s, version, clause)
        await s.commit()
        assert await ToolBox(s, "SG")._load_candidates() == []
        assert not await layout_held_clause_ids(s, [clauses[0].id])


async def test_report_export_blocked_but_historical_content_remains_unchanged(
    client, seed, session_factory
):
    async with session_factory() as s:
        _, version, clauses = await make_document(s)
        clauses[0].page_start = clauses[0].page_end = 1
        task = AnalysisTask(
            org_id=seed["org_a"].id,
            created_by=seed["a_member"].id,
            title="old report",
            country_code="SG",
            vehicle_model="test",
            scenario_type="test",
            status="completed",
        )
        s.add(task)
        await s.flush()
        dims = [
            {
                "code": "D1",
                "title": "旧章节",
                "content": "旧正文必须保留",
                "citations": [{"clause_id": str(clauses[0].id)}],
            }
        ]
        rv = ReportVersion(
            task_id=task.id,
            version_no=1,
            executive_summary="旧摘要",
            change_note="old",
            dimensions_json=copy.deepcopy(dims),
        )
        s.add(rv)
        await page_proposal(s, version, clauses[0])
        await s.commit()
        task_id, report_id, clause_id = str(task.id), rv.id, str(clauses[0].id)
        before = record_payload(rv)
    user = bearer(await login(client, "user", "a-member@example.com"))
    other = bearer(await login(client, "user", "b-admin@example.com"))
    for extension in ("docx", "pdf"):
        response = await client.get(
            f"/api/analysis/tasks/{task_id}/reports/1/export.{extension}", headers=user
        )
        assert response.status_code == 409 and "解析" in response.json()["detail"]
        assert (
            await client.get(
                f"/api/analysis/tasks/{task_id}/reports/1/export.{extension}", headers=other
            )
        ).status_code == 404
    card = (
        await client.get(f"/api/analysis/clauses/{clause_id}/citation-card", headers=user)
    ).json()
    assert card["source_integrity_alerts"] and not card["usable_as_legal_basis"]
    assert card["original_text"] and card["related_requirements"] == []
    async with session_factory() as s:
        assert record_payload(await s.get(ReportVersion, report_id)) == before
