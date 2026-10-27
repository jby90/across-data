import pytest
from sqlalchemy import func, select

from app.analysis.tools import ToolBox
from app.knowledge import extractor
from app.knowledge.models import KgEdge, KnowledgeAuditRecord, RelationType

from .review_s1_s2_contracts import make_document, make_edge


async def test_failed_judge_rolls_back_metadata_and_old_edges(session_factory, monkeypatch):
    async def extracted(**kwargs):
        quote = "An organisation must protect personal data."
        return extractor.ClauseExtractionPayload(
            entities=[
                extractor.ExtractedEntity(
                    type="OBLIGATION", name_zh="个人数据保护", name_orig="protect personal data"
                )
            ],
            relations=[
                extractor.ExtractedRelation(
                    type="REQUIRES",
                    source=kwargs["document_title"],
                    target="个人数据保护",
                    evidence_quote=quote,
                )
            ],
        ), "fake"

    async def unavailable(**kwargs):
        return None

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", extracted)
    monkeypatch.setattr(extractor, "_call_llm_judge", unavailable)
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0])
        await session.commit()
        result = await extractor.extract_knowledge_for_country(session, "SG")
        assert result["status"] == "failed" and result["previous_graph_preserved"]
        await session.refresh(edge)
        assert edge.review_status == "pending_review"
        assert await session.scalar(select(func.count()).select_from(KgEdge)) == 1
        audit = await session.scalar(
            select(KnowledgeAuditRecord).where(
                KnowledgeAuditRecord.action == "graph_rebuild_result"
            )
        )
        assert audit.payload["status"] == "failed"


@pytest.mark.parametrize("change", ["unjudged", "bad_quote", "out_of_scope"])
async def test_agent_tool_rejects_unreliable_current_edges(session_factory, change):
    async with session_factory() as session:
        doc, _, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0])
        if change == "unjudged":
            edge.judge_verdict = "unavailable"
        elif change == "bad_quote":
            edge.evidence_quote = "This is not the original source."
        else:
            doc.relevance = "out_of_scope"
        await session.commit()
        assert await ToolBox(session, "SG").query_kg_obligations([str(clauses[0].id)]) == []


async def test_pending_evidence_revision_is_a_new_row(session_factory):
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, source, target = await make_edge(session, clauses[0])
        old_quote = edge.evidence_quote
        await session.commit()
        new = await extractor._upsert_edge(
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
        await session.commit()
        assert edge.id != new.id and edge.evidence_quote == old_quote
        assert edge.review_status == "superseded" and new.review_status == "pending_review"


def test_fuzzy_quote_cannot_drop_negation_and_fuzzy_law_titles_do_not_resolve():
    original = "An organisation must not delete personal data required for legal proceedings."
    invented = "An organisation must delete personal data required for legal proceedings."
    assert extractor.align_verbatim_quote(original, invented) is None
    from types import SimpleNamespace

    node = SimpleNamespace(id="law")
    index = [("personal data protection act 2012", node, SimpleNamespace())]
    assert extractor._resolve_instrument_target("Personal Data Protection Act 2013", index) is None


async def test_real_judge_adapter_requires_reason_and_records_response_model(monkeypatch):
    import json

    import httpx

    from app.core.config import get_settings
    from app.knowledge.models import NodeType

    monkeypatch.setattr(get_settings(), "llm_api_key", "test-only")
    payload = {"supports": "yes", "reason": "The quote directly requires personal data protection."}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "actual-judge-version",
                "choices": [{"message": {"content": json.dumps(payload)}}],
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        extractor.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    kwargs = dict(
        relation_type=RelationType.REQUIRES,
        source_name="Law",
        source_type=NodeType.INSTRUMENT,
        target_name="Protect personal data",
        target_type=NodeType.OBLIGATION,
        evidence_quote="An organisation must protect personal data.",
    )
    result = await extractor._call_llm_judge(**kwargs)
    assert result.supports == "yes" and result.model_name == "actual-judge-version"
    payload.pop("reason")
    assert await extractor._call_llm_judge(**kwargs) is None
