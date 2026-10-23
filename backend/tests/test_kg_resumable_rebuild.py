from collections import Counter

from sqlalchemy import select

from app.core.config import get_settings
from app.knowledge import extractor
from app.knowledge.models import KgEdge, KgNode, KnowledgeAuditRecord, KnowledgeChangeProposal

from .review_s1_s2_contracts import make_document, make_edge


async def test_schema_retry_regenerates_long_names_instead_of_truncating(monkeypatch):
    import json

    import httpx

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")
    client_type, requests = httpx.AsyncClient, []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        original = "A" * 256 if len(requests) == 1 else "protection obligation"
        if len(requests) == 2:
            assert "string_too_long" in body["messages"][-1]["content"]
            assert "255" in body["messages"][-1]["content"]
            assert body["messages"][-2]["role"] == "assistant"
            assert "A" * 256 in body["messages"][-2]["content"]
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "entities": [
                                        {
                                            "type": "OBLIGATION",
                                            "name_zh": "保护义务",
                                            "name_orig": original,
                                            "definition": "Complete original description remains here.",
                                        }
                                    ],
                                    "relations": [],
                                }
                            )
                        }
                    }
                ],
            },
        )

    monkeypatch.setattr(
        extractor.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = await extractor._call_llm_extract_clause(
        "An organisation must protect personal data.", "1", "Law", "Regulator", "MA"
    )
    assert result is not None and len(requests) == 2
    assert result[0].entities[0].name_orig == "protection obligation"
    assert result[0].entities[0].definition == "Complete original description remains here."
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        extractor.ExtractedEntity(type="OBLIGATION", name_zh="保护义务", name_orig="A" * 256)


async def test_extraction_mode_is_separate_from_independent_judge_and_usage_is_recorded(
    monkeypatch,
):
    import json

    import httpx

    from app.knowledge.kg_run_state import begin_usage, end_usage
    from app.knowledge.models import NodeType, RelationType

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_kg_extraction_thinking", "disabled")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")
    client_type = httpx.AsyncClient
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("thinking") == {"type": "disabled"}:
            assert "reasoning_effort" not in body
            payload = {"entities": [], "relations": []}
        else:
            assert body["reasoning_effort"] == "low"
            payload = {"supports": "yes", "reason": "The original explicitly requires safeguards."}
        return httpx.Response(
            200,
            json={
                "model": "actual-response-model",
                "choices": [{"message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22},
            },
        )

    monkeypatch.setattr(
        extractor.httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    token, usage = begin_usage()
    try:
        assert await extractor._call_llm_extract_clause(
            clause_text="Protect personal data.",
            clause_number="1",
            document_title="Law",
            publisher_name="Regulator",
            country_code="SG",
        )
        assert await extractor._call_llm_judge(
            relation_type=RelationType.REQUIRES,
            source_name="Law",
            source_type=NodeType.INSTRUMENT,
            target_name="Safeguards",
            target_type=NodeType.OBLIGATION,
            evidence_quote="Protect personal data.",
        )
    finally:
        end_usage(token)
    assert len(calls) == 2 and usage["requests"] == usage["responses_with_usage"] == 2
    assert usage["tokens_in"] == 22 and usage["tokens_out"] == 44
    assert usage["response_models"] == {"actual-response-model": 2}


async def test_retry_reuses_only_complete_verified_source_results_and_keeps_human_approval(
    session_factory,
    monkeypatch,
):
    counts = Counter()
    fail_second = True

    async def extract(**kwargs):
        text = kwargs["clause_text"]
        counts[text] += 1
        if text.startswith("Second") and fail_second:
            return None
        return extractor.ClauseExtractionPayload(entities=[], relations=[]), "test-model"

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", extract)
    async with session_factory() as session:
        _, _, clauses = await make_document(
            session,
            [
                "An organisation must protect personal data. Further safeguards apply.",
                "Second provision requires retention safeguards. It protects personal data.",
            ],
        )
        old, _, _ = await make_edge(session, clauses[0], "approved")
        await session.commit()
        protected = (old.evidence_quote, old.review_status, old.reviewed_at)
        current_prompt = extractor.EXTRACTOR_PROMPT_VERSION
        monkeypatch.setattr(extractor, "EXTRACTOR_PROMPT_VERSION", "kg-extract-v2")
        first = await extractor.extract_knowledge_for_country(session, "SG")
        assert first["status"] == "failed" and first["previous_graph_preserved"]
        assert await session.scalar(
            select(KnowledgeAuditRecord.id).where(KnowledgeAuditRecord.action == "kg_clause_result")
        )
        fail_second = False
        monkeypatch.setattr(extractor, "EXTRACTOR_PROMPT_VERSION", current_prompt)
        second = await extractor.extract_knowledge_for_country(session, "SG")
        assert second["status"] == "completed" and second["attempted_clauses"] == 2
        assert second["cache_hits"] == 1
        assert second["cached_prompt_versions"] == {"kg-extract-v2": 1}
        assert counts[clauses[0].original_text] == 1 and counts[clauses[1].original_text] == 2
        await session.refresh(old)
        assert protected == (old.evidence_quote, old.review_status, old.reviewed_at)
        nodes = (
            await session.scalars(
                select(KgNode).where(KgNode.id.notin_([old.source_node_id, old.target_node_id]))
            )
        ).all()
        assert all(row.review_status == "pending_review" for row in nodes)
        edges = (await session.scalars(select(KgEdge).where(KgEdge.id != old.id))).all()
        assert all(row.review_status == "pending_review" for row in edges)
        monkeypatch.setattr(get_settings(), "llm_model", "changed-model")
        third = await extractor.extract_knowledge_for_country(session, "SG")
        assert third["cache_hits"] == 0
        assert counts[clauses[0].original_text] == 2


def test_entity_normalization_does_not_add_unstated_scope_or_merge_distinct_data():
    from app.knowledge.models import NodeType

    key, zh, _ = extractor.normalize_entity_node_key(
        NodeType.DATA_CATEGORY, "敏感个人数据", "Sensitive Personal Data"
    )
    assert key == "data:sensitive_personal_data_literal" and zh == "敏感个人数据"
    assert "生物识别" not in zh and "位置" not in zh
    keys = set()
    for zh, original in [
        ("位置敏感个人数据", "sensitive location data"),
        ("车载数据记录器", "data recorder"),
        ("自动驾驶传感器数据", "AV sensor data"),
        ("物联网数据", "IoT"),
        ("车联网数据", "telematics"),
        ("数据泄露", "data breach"),
    ]:
        node_key, label, orig = extractor.normalize_entity_node_key(
            NodeType.DATA_CATEGORY, zh, original
        )
        assert label == zh and orig == original
        keys.add(node_key)
    assert len(keys) == 6


async def test_successful_rebuild_proposes_scope_changes_without_overwriting_metadata(
    session_factory,
    monkeypatch,
):
    async def classify(**kwargs):
        return extractor.DocumentRelevanceVerdict(relevance="relevant", reason="new model reason")

    async def extract(**kwargs):
        return extractor.ClauseExtractionPayload(entities=[], relations=[]), "test-model"

    monkeypatch.setattr(extractor, "_call_llm_doc_relevance", classify)
    monkeypatch.setattr(extractor, "_call_llm_extract_clause", extract)
    async with session_factory() as session:
        doc, _, _ = await make_document(session)
        doc.relevance_source, doc.relevance_note = "rule", "original classification"
        await session.commit()
        before = (doc.relevance, doc.relevance_note, doc.relevance_source)
        result = await extractor.extract_knowledge_for_country(session, "SG")
        assert result["status"] == "completed"
        await session.refresh(doc)
        assert before == (doc.relevance, doc.relevance_note, doc.relevance_source)
        assert await session.scalar(
            select(KnowledgeChangeProposal.id).where(KnowledgeChangeProposal.target_id == doc.id)
        )
