import re

from sqlalchemy import func, select

from app.knowledge import translator
from app.knowledge.models import ClauseTranslation, KnowledgeAuditRecord
from app.knowledge.translation_context import translate_fragment_context

from .review_s1_s2_contracts import make_document, make_edge


async def test_context_preserves_fragment_evidence_and_approval_and_validates_real_context(
    session_factory,
    monkeypatch,
):
    calls = 0

    async def translate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return "组织必须保护个人数据并向委员会发出通知。\n[40/2020]", "test-model", 20, 30, 40

    monkeypatch.setattr(translator, "_call_llm_translate", translate)
    async with session_factory() as session:
        _, _, clauses = await make_document(
            session,
            ["Organisations must protect personal data and notify the Commission.", "[40/2020]"],
        )
        edge, _, _ = await make_edge(session, clauses[1], "approved")
        previous = ClauseTranslation(
            clause_id=clauses[1].id,
            translated_text="[40/2020]",
            engine="llm",
            model_name="old",
            review_status="translation_failed",
        )
        session.add(previous)
        await session.commit()
        before = (
            clauses[1].original_text,
            clauses[1].text_hash,
            edge.evidence_quote,
            edge.review_status,
        )
        revision = await translate_fragment_context(session, clauses[1])
        again = await translate_fragment_context(session, clauses[1])
        await session.commit()
        assert calls == 1 and revision.id == again.id
        assert revision.payload["quality_check_passed"]
        assert revision.payload["review_status"] == "pending_review"
        assert not revision.payload["standalone_translation"]
        assert not revision.payload["usable_as_legal_basis"]
        combined = revision.payload["combined_source_text"]
        for source, span in zip(clauses, revision.payload["source_spans"], strict=True):
            assert combined[span["start"] : span["end"]] == source.original_text
        assert before == (
            clauses[1].original_text,
            clauses[1].text_hash,
            edge.evidence_quote,
            edge.review_status,
        )
        assert (
            previous.review_status == "translation_failed"
            and previous.translated_text == "[40/2020]"
        )
        assert await session.scalar(select(func.count()).select_from(KnowledgeAuditRecord)) == 1
        assert not translator.verify_translation("[40/2020]", "[40/2020]", [])[0]


def test_trailing_fragment_chunk_keeps_real_context_without_dropping_any_text():
    source = (
        "Organisations must protect personal data and follow the applicable safeguards. " * 19
    ) + "\n[40/2020]"
    chunks = translator.split_clause_into_chunks(source)
    assert len(chunks) > 1 and len(chunks[-1]) > 120
    assert all(len(chunk) <= translator.MAX_CHUNK_CHARS for chunk in chunks)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", source)


def test_arabic_prefix_percentage_and_comma_date_keep_value_and_identity():
    from app.knowledge.numeric_integrity import dates, tokens

    source = "في ١٢ يوليو ،٢٠٢٣ لا يتجاوز ٪٢٠"
    assert dates(source) == {(2023, 7, 12)}
    assert tokens(source) == {("12", False), ("2023", False), ("20", True)}
    safe, mapping = translator._protect_explicit_dates(source)
    assert list(mapping.values()) == ["2023年7月12日"]
    _, numbers = translator._protect_numeric_literals(safe)
    assert "٪٢٠" in numbers.values()
    assert translator.verify_translation(source, "在2023年7月12日，不得超过百分之20。", [])[0]
    assert not translator.verify_translation(source, "在2023年7月12日，不得超过20。", [])[0]
    assert not translator.verify_translation(source, "在2023年7月12日，不得超过21%。", [])[0]
    assert not translator.verify_translation(source, "在2023年12月7日，不得超过20%。", [])[0]


async def test_density_context_cannot_hide_a_missing_number(session_factory):
    import pytest

    async with session_factory() as session:
        _, _, clauses = await make_document(
            session, ["Organisation must protect data. " * 12, "4. 100,000 $0.012 per number"]
        )
        session.add(
            ClauseTranslation(
                clause_id=clauses[1].id,
                translated_text="缺失数字",
                engine="llm",
                model_name="old",
                is_current=True,
                review_status="translation_failed",
                quality_issues=["译文缺失原文数字或比例: 100000"],
            )
        )
        await session.flush()
        with pytest.raises(ValueError, match="context requires"):
            await translate_fragment_context(session, clauses[1], density_context=True)
