import copy

import pytest
from sqlalchemy import func, select

from app.ingestion.archive import ArchiveStore
from app.knowledge import extractor, parsed_graph
from app.knowledge.history import payload_hash, record_payload
from app.knowledge.models import KgEdge, KnowledgeAuditRecord

from .conftest import bearer, login
from .review_s1_s2_contracts import make_document, make_edge
from .test_source_layout_repair import prepared_table


async def source_fixture(s, archive):
    doc, version, clauses = await make_document(s, ["7. 100,001 and above $3,200"])
    version.content_hash, version.archive_path = archive.put(b"frozen original PDF")
    clause = clauses[0]
    clause.page_start = clause.page_end = 1
    prepared = prepared_table()
    payload = dict(
        version_id=str(version.id),
        pdf_content_hash=version.content_hash,
        original_clause_text=clause.original_text,
        original_clause_text_hash=clause.text_hash,
        page_number=1,
        prepared=prepared,
        active=False,
        human_reviewed=False,
        usable_as_legal_basis=False,
        review_status="pending_review",
    )
    source = KnowledgeAuditRecord(
        target_type="clauses",
        target_id=clause.id,
        action="source_parse_revision",
        payload=payload,
        content_hash=payload_hash(payload),
    )
    s.add(source)
    await s.flush()
    return source, prepared, doc, clause


async def test_pending_table_graph_records_evidence_without_activating_or_inheriting_approval(
    session_factory, monkeypatch, tmp_path
):
    calls, judges = [], []

    async def extract(**kwargs):
        text = kwargs["clause_text"]
        calls.append(text)
        assert "Made this" not in text
        return extractor.ClauseExtractionPayload.model_validate(
            dict(
                entities=[
                    dict(type="OBLIGATION", name_zh="申请费预付", name_orig="advance payment")
                ],
                relations=[
                    dict(
                        type="REQUIRES",
                        source=kwargs["document_title"],
                        target="申请费预付",
                        evidence_quote=text[:100],
                        confidence=0.9,
                    )
                ],
            )
        ), "test-extractor"

    async def judge(**kwargs):
        judges.append(kwargs)
        return extractor.SemanticJudgeVerdict(
            supports="yes", reason="fixture independent judge", model_name="test-judge"
        )

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", extract)
    monkeypatch.setattr(extractor, "judge_extracted_relation", judge)
    async with session_factory() as s:
        archive = ArchiveStore(tmp_path)
        source, prepared, doc, clause = await source_fixture(s, archive)
        monkeypatch.setattr(parsed_graph, "prepare_page", lambda *args: prepared)
        old_edge, _, _ = await make_edge(s, clause, "approved")
        await s.commit()
        before = record_payload(old_edge)
        result, reused = await parsed_graph.propose_parsed_graph(s, source, archive)
        await s.commit()
        assert len(calls) == 2 and len(judges) == 2 and not reused
        assert result.payload["status"] == "completed"
        assert result.payload["review_status"] == "pending_review" and not result.payload["active"]
        assert not result.payload["usable_as_legal_basis"] and not result.payload["human_reviewed"]
        assert sum(len(b["relations"]) for b in result.payload["blocks"]) == 2
        for block in result.payload["blocks"]:
            for rel in block["relations"]:
                assert rel["evaluation"]["admitted"]
                assert rel["evaluation"]["aligned_quote"] in block["source_text"]
        again, cached = await parsed_graph.propose_parsed_graph(s, source, archive)
        assert cached and again.id == result.id and len(calls) == len(judges) == 2
        assert await s.scalar(select(func.count()).select_from(KgEdge)) == 1
        await s.refresh(old_edge)
        assert record_payload(old_edge) == before
        damaged = copy.deepcopy(result.payload)
        damaged["blocks"][1]["relations"][0]["quote_span"]["start"] += 1
        with pytest.raises(ValueError, match="span differs"):
            parsed_graph.validate_cached_result(damaged, source, prepared)
        damaged = copy.deepcopy(result.payload)
        damaged["blocks"][1] = damaged["blocks"][0]
        with pytest.raises(ValueError, match="duplicated"):
            parsed_graph.validate_cached_result(damaged, source, prepared)


async def test_bibliography_is_not_sent_to_model_or_promoted_as_foreign_law(
    session_factory, monkeypatch, tmp_path
):
    from app.ingestion.source_layout import partition_page

    async def forbidden(**kwargs):
        raise AssertionError("bibliography cannot be treated as Egyptian legal obligations")

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", forbidden)
    async with session_factory() as s:
        archive = ArchiveStore(tmp_path)
        source, _, _, _ = await source_fixture(s, archive)
        text = "33\n" + "\n".join(
            f"{i} - General Data Regulation 2016, https://example.org/{i}" for i in range(14, 17)
        )
        prepared = dict(layout=dict(text=text, glyph_coverage_passed=True), **partition_page(text))
        source.payload = {**source.payload, "prepared": prepared}
        source.content_hash = payload_hash(source.payload)
        monkeypatch.setattr(parsed_graph, "prepare_page", lambda *args: prepared)
        result, _ = await parsed_graph.propose_parsed_graph(s, source, archive)
        assert result.payload["status"] == "completed"
        assert all(
            b["status"] == "excluded_non_normative" and not b["relations"]
            for b in result.payload["blocks"]
        )


async def test_fees_are_not_penalties_and_model_failure_does_not_replace_previous_candidates(
    session_factory, monkeypatch, tmp_path
):
    calls = 0

    async def extract(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 2:
            return None
        return extractor.ClauseExtractionPayload.model_validate(
            dict(
                entities=[dict(type="PENALTY", name_zh="罚款100", name_orig="fine100")],
                relations=[
                    dict(
                        type="PENALIZES",
                        source=kwargs["document_title"],
                        target="罚款100",
                        evidence_quote="$100",
                    )
                ],
            )
        ), "test-model"

    async def forbidden_judge(**kwargs):
        raise AssertionError("fee-as-penalty must fail before semantic judge")

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", extract)
    monkeypatch.setattr(extractor, "judge_extracted_relation", forbidden_judge)
    async with session_factory() as s:
        archive = ArchiveStore(tmp_path)
        source, prepared, _, _ = await source_fixture(s, archive)
        monkeypatch.setattr(parsed_graph, "prepare_page", lambda *args: prepared)
        first, _ = await parsed_graph.propose_parsed_graph(s, source, archive)
        await s.commit()
        first_payload = copy.deepcopy(first.payload)
        assert all(
            not rel["evaluation"]["admitted"]
            for b in first.payload["blocks"]
            for rel in b["relations"]
        )
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "llm_model", "changed-model")
        second, reused = await parsed_graph.propose_parsed_graph(s, source, archive)
        await s.commit()
        assert not reused and second.id != first.id and second.payload["status"] == "failed"
        assert second.payload["review_status"] == "pending_review"
        await s.refresh(first)
        assert first.payload == first_payload


async def test_graph_proposals_are_admin_review_only(client, seed, session_factory):
    payload = dict(
        country_code="SG",
        review_status="pending_review",
        active=False,
        human_reviewed=False,
        usable_as_legal_basis=False,
        blocks=[],
    )
    async with session_factory() as s:
        _, _, clauses = await make_document(s)
        s.add(
            KnowledgeAuditRecord(
                target_type="clauses",
                target_id=clauses[0].id,
                action="kg_parsed_source_result",
                payload=payload,
                content_hash=payload_hash(payload),
            )
        )
        await s.commit()
    admin = bearer(await login(client, "admin", "admin@example.com"))
    user = bearer(await login(client, "user", "a-member@example.com"))
    assert (
        await client.get(
            "/api/admin/knowledge/parsed-graph-revisions?country_code=SG", headers=user
        )
    ).status_code == 401
    response = await client.get(
        "/api/admin/knowledge/parsed-graph-revisions?country_code=SG", headers=admin
    )
    assert response.status_code == 200
    assert response.json()[0]["payload"] == payload
    assert (
        await client.get(
            "/api/admin/knowledge/parsed-graph-revisions?country_code=EG", headers=admin
        )
    ).json() == []
