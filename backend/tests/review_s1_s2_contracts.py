"""Independent review contracts for S1/S2, not product implementation tests.

Run explicitly: pytest -q tests/review_s1_s2_contracts.py
The assertions describe acceptance requirements. Failures reproduce defects;
the production code is intentionally left unchanged during this review.
All writes use conftest's temporary PostgreSQL instance, with mocked LLMs.
"""

import hashlib

import pytest
from sqlalchemy import func, select

from app.analysis.tools import ToolBox
from app.cli import _split_oversized_clauses_for_country, batch_translate
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge import extractor, translator
from app.knowledge.models import ClauseTranslation, KgEdge, KgNode, RelationType
from app.sources.models import SourceSite


async def make_document(session, texts=None):
    site = SourceSite(
        country_code="SG",
        publisher="Review fixture",
        language="en",
        document_type="LAW",
        entry_url="https://example.org/review-only",
        strategy="DIRECT",
        terms_status="pending",
        enabled=False,
    )
    session.add(site)
    await session.flush()
    doc = Document(
        site_id=site.id,
        country_code="SG",
        title="Personal Data Protection Act 2012",
        document_type="LAW",
        language="en",
        source_url="https://example.org/review-only",
        relevance="relevant",
        relevance_source="manual",
    )
    session.add(doc)
    await session.flush()
    version = DocumentVersion(
        document_id=doc.id,
        version_no=1,
        content_hash="a" * 64,
        archive_path="SG/review-only.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        final_url=doc.source_url,
        state="parsed",
    )
    session.add(version)
    await session.flush()
    clauses = []
    for index, text in enumerate(
        texts
        or [
            "An organisation must protect personal data. "
            "An organisation must notify the Commission of a breach."
        ],
        1,
    ):
        clause = Clause(
            version_id=version.id,
            ordinal=index,
            clause_number=str(index),
            language="en",
            original_text=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
        session.add(clause)
        clauses.append(clause)
    await session.flush()
    return doc, version, clauses


async def make_edge(session, clause, status="pending_review"):
    source = KgNode(
        country_code="SG",
        node_type="INSTRUMENT",
        node_key="review:law",
        name_zh="测试法规",
        review_status="approved",
    )
    target = KgNode(
        country_code="SG",
        node_type="OBLIGATION",
        node_key="review:obligation",
        name_zh="个人数据保护义务",
        review_status="pending_review",
    )
    session.add_all([source, target])
    await session.flush()
    edge = KgEdge(
        country_code="SG",
        source_node_id=source.id,
        target_node_id=target.id,
        relation_type="REQUIRES",
        evidence_clause_id=clause.id,
        evidence_quote=clause.original_text.split(". ")[0] + ".",
        extractor_model="review-llm",
        confidence=0.9,
        judge_verdict="yes",
        is_structural=False,
        review_status=status,
    )
    session.add(edge)
    await session.flush()
    return edge, source, target


@pytest.mark.parametrize(
    "original,wrong_translation",
    [
        ("The fine is 10 and the period is 5 days.", "罚款为100元，期限为50天。"),
        ("The maximum percentage is 10%.", "最高比例为10，计算方式另定。"),
        ("The fee is $500.", "费用为500欧元，应按规定缴纳。"),
    ],
)
def test_s1_numeric_gate_rejects_changed_values_and_units(original, wrong_translation):
    passed, issues, _ = translator.verify_translation(original, wrong_translation, [])
    assert not passed, f"Incorrect values/units were accepted: {wrong_translation}; issues={issues}"


async def test_s2_judge_outage_must_not_admit_unjudged_wrong_target(monkeypatch):
    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(extractor, "_call_llm_judge", unavailable)
    quote = "An organisation must protect personal data."
    result = await extractor.evaluate_kg_candidate(
        relation_type="REQUIRES",
        source_type="INSTRUMENT",
        source_name="Test law",
        target_type="OBLIGATION",
        target_name="Delete all recordings within 30 days",
        evidence_quote=quote,
        clause_original_text=quote,
        document_title="Personal Data Protection Act 2012",
        use_llm_judge=True,
    )
    assert not result.admitted, result.model_dump()


@pytest.mark.parametrize("status", ["superseded", "rejected", "stale"])
async def test_s2_analysis_tool_must_exclude_retired_edges(session_factory, status):
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0], status)
        await session.commit()
        results = await ToolBox(session, "SG").query_kg_obligations([str(clauses[0].id)])
        assert results == [], f"Retired edge {edge.id} leaked into agent results: {results}"


async def test_s2_failed_rebuild_preserves_previous_pending_semantic_graph(
    session_factory, monkeypatch
):
    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(extractor, "_call_llm_extract_clause", unavailable)
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, _, _ = await make_edge(session, clauses[0])
        await session.commit()
        result = await extractor.extract_knowledge_for_country(
            session, "SG", translate_sample_per_doc=0
        )
        await session.refresh(edge)
        assert edge.review_status == "pending_review", (
            f"Extraction returned {result}, but previous semantic edge became {edge.review_status}"
        )


async def test_s2_reextract_cannot_change_evidence_while_retaining_human_approval(session_factory):
    async with session_factory() as session:
        _, _, clauses = await make_document(session)
        edge, source, target = await make_edge(session, clauses[0], "approved")
        old_quote = edge.evidence_quote
        await session.commit()
        updated = await extractor._upsert_edge(
            session,
            country_code="SG",
            source_node=source,
            target_node=target,
            relation_type=RelationType.REQUIRES,
            evidence_clause=clauses[0],
            evidence_quote="An organisation must notify the Commission of a breach.",
            extractor_model="different-model",
            confidence=0.7,
            judge_verdict="yes",
            review_status="pending_review",
        )
        assert updated is not None
        assert updated.evidence_quote == old_quote or updated.review_status != "approved", (
            "Evidence changed while the previous human-approved status remained"
        )


async def test_s1_translation_must_not_rewrite_original_clauses_or_edge_evidence(session_factory):
    text = "First paragraph " + "A" * 1250 + ".\nSecond paragraph " + "B" * 1250 + "."
    async with session_factory() as session:
        _, _, clauses = await make_document(session, [text])
        clause = clauses[0]
        edge, _, _ = await make_edge(session, clause, "approved")
        edge.evidence_quote = text[1250:1300]  # genuine quote crossing the chunk boundary
        old_quote = edge.evidence_quote
        old_hash = clause.text_hash
        await session.commit()
        count = await _split_oversized_clauses_for_country(session, "SG")
        await session.refresh(clause)
        await session.refresh(edge)
        observed = {
            "split_count": count,
            "original_unchanged": clause.original_text == text,
            "hash_unchanged": clause.text_hash == old_hash,
            "edge_quote_unchanged": edge.evidence_quote == old_quote,
            "edge_status": edge.review_status,
        }
        assert all(
            observed[key]
            for key in ("original_unchanged", "hash_unchanged", "edge_quote_unchanged")
        ), observed


async def test_s1_only_missing_should_cover_all_clauses_not_first_eight(
    session_factory, monkeypatch
):
    async def translated(text, *args, **kwargs):
        import re

        number = re.search(r"\d+", text).group()
        return (f"第{number}条：组织应当保护个人数据并履行法定义务。", "test-llm", 30, 20, 1)

    monkeypatch.setattr(translator, "_call_llm_translate", translated)
    async with session_factory() as session:
        _, _, clauses = await make_document(
            session,
            [f"Clause {i}: An organisation must protect personal data." for i in range(1, 21)],
        )
        await session.commit()
        clause_ids = [clause.id for clause in clauses]
    result = await batch_translate("SG", only_missing=True, concurrency=2, sf=session_factory)
    async with session_factory() as session:
        done = await session.scalar(
            select(func.count())
            .select_from(ClauseTranslation)
            .where(
                ClauseTranslation.clause_id.in_(clause_ids),
                ClauseTranslation.quality_check_passed.is_(True),
                ClauseTranslation.review_status == "machine_translated",
            )
        )
        assert done == 20, f"Only {done}/20 clauses translated; batch statistics: {result}"


def test_s2_node_eight_attributes_must_not_invent_facts_for_out_of_scope_file():
    from app.knowledge.router import _compute_eight_attributes

    attributes = _compute_eight_attributes(
        "SG",
        title="Courts (Civil and Criminal Justice) Reform Act 2021",
        rel_types=set(),
        data_names=[],
        obl_names=[],
    )
    assert not any("PDPA" in value or "3 年" in value for value in attributes.values()), attributes
