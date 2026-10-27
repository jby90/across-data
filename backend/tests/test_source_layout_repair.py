import copy
import hashlib

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import func, select

from app.ingestion.archive import ArchiveStore
from app.ingestion.clauses import split_clauses
from app.ingestion.source_layout import (
    atomic_line_blocks,
    ltr_page,
    partition_page,
    resolve_unmapped_hyphens,
)
from app.knowledge import source_repairs, translator
from app.knowledge.history import payload_hash, propose, record_payload
from app.knowledge.models import ClauseTranslation, KnowledgeAuditRecord
from app.knowledge.numeric_integrity import dates

from .review_s1_s2_contracts import make_document, make_edge

TABLE = (
    "S 709/2013 16\nSECOND SCHEDULE\nRegulation 10(3)\n"
    "ADVANCE PAYMENT OF APPLICATION FEE\nFirst column\nSecond column\n"
    "Number of Singapore telephone numbers pre-paid at any one time\nAmount\n"
    "1. 5,000 $100\n2. 10,000 $150\n"
    "THIRD SCHEDULE\nRegulation 14(1)\nFEES PAYABLE IN RESPECT OF TERMINATED NUMBERS\n"
    "First column\nSecond column\nNumber of terminated Singapore telephone numbers\nFee\n"
    "1. 1 – 20 $100\n2. 100,001 and above $3,200\n"
    "Made this 21st day of November 2013.\nAUBECK KAM\nPermanent Secretary,\n"
    "Ministry of Communications and Information, Singapore.\n[Y03.002.001.EV30/13]\n"
)


def prepared_table():
    return dict(
        layout=dict(
            text=TABLE, raw_pdf_text=TABLE, glyph_coverage_passed=True, engine="test-layout"
        ),
        **partition_page(TABLE),
    )


class GlyphPage:
    def __init__(self, text):
        self.text = text

    def get_text_range(self, start=None, count=None):
        return self.text if start is None else self.text[start : start + count]

    def count_chars(self):
        return len(self.text)

    def get_charbox(self, index, loose=False):
        # The PDF content stream is reversed; the visual page is ordinary LTR.
        x = (len(self.text) - index - 1) * 5
        return x, 10, x + 5, 20


def test_geometric_order_preserves_every_character_and_refuses_mixed_language():
    result = ltr_page(GlyphPage(".noitcetorP ataD"))
    assert result["text"] == "Data Protection."
    assert result["raw_pdf_text"] == ".noitcetorP ataD"
    assert result["glyph_coverage_passed"]
    for index, position in result["glyph_offsets"].items():
        assert result["text"][position] == result["raw_pdf_text"][int(index)]
    with pytest.raises(ValueError, match="mixed/Arabic"):
        ltr_page(GlyphPage("البيانات الشخصية Data"))
    broken = GlyphPage(".noitcetorP ataD")
    broken.count_chars = lambda: len(broken.text) - 1
    with pytest.raises(ValueError, match="coverage"):
        ltr_page(broken)


def test_unmapped_glyph_needs_identical_same_page_visual_exemplars():
    class Page:
        def get_height(self):
            return 40

        def render(self, scale):
            class Bitmap:
                def to_pil(self):
                    image = Image.new("L", (500, 480), 255)
                    draw = ImageDraw.Draw(image)
                    for x in (24, 144, 264):
                        draw.rectangle((x, 240, x + 24, 248), fill=0)
                    return image

                def close(self):
                    pass

            return Bitmap()

    glyphs = GlyphPage("--\ufffe")
    glyphs.get_charbox = lambda index, loose=False: (2 + 10 * index, 19, 4 + 10 * index, 21)
    layout = dict(text="--\ufffe", glyph_offsets={"0": 0, "1": 1, "2": 2})
    decoded = resolve_unmapped_hyphens(Page(), glyphs, layout)
    assert decoded["raw_layout_text"] == "--\ufffe"
    assert decoded["text"] == "---"
    assert decoded["glyph_decodings"][0]["exemplar_glyph_indices"] == [0, 1]
    glyphs.text = "-x\ufffe"
    with pytest.raises(ValueError, match="decoding proof"):
        resolve_unmapped_hyphens(Page(), glyphs, layout)


def test_complete_tables_are_atomic_and_signature_is_not_last_row():
    drafts, strategy = split_clauses([TABLE])
    assert strategy == "layout"
    tables = [d for d in drafts if (d.section_path or "").startswith("TABLE /")]
    assert len(tables) == 2
    assert "Regulation 10(3)" in tables[0].text
    assert "Number of terminated" in tables[1].text
    assert "2. 100,001 and above $3,200" in tables[1].text
    assert "Made this" not in tables[1].text
    assert all(d.clause_number is None for d in tables)
    assert drafts[-1].section_path == "SIGNATURE"
    assert "AUBECK KAM" in drafts[-1].text
    assert all(d.page_start == d.page_end == 1 for d in drafts)
    ordinary = "FIRST SCHEDULE\n1. The controller must notify.\n2. The processor must protect."
    assert atomic_line_blocks(ordinary) == {}
    normal, strategy = split_clauses([ordinary])
    assert strategy == "numbered" and [d.clause_number for d in normal] == ["1", "2"]


def test_partition_preserves_every_row_name_id_and_wrapped_url():
    prepared = prepared_table()
    assert "".join(p["source_text"] for p in prepared["pieces"]) == TABLE
    assert [
        p["source_text"] for p in prepared["literal_fields"] if p["kind"] == "signatory_name"
    ] == ["AUBECK KAM"]
    assert any(p["source_text"] == "[Y03.002.001.EV30/13]" for p in prepared["literal_fields"])
    assert any(u["source_text"] == "and above" for u in prepared["translation_units"])
    refs = "33\n" + "\n".join(
        f"{n} - The Regulation, 27 April 2016, retrieved from https://\neur-lex.europa.eu/legal-\ncontent/EN/TXT/?uri=CELEX:{n}"
        for n in range(14, 17)
    )
    result = partition_page(refs)
    assert len(result["translation_units"]) == 3
    urls = [p for p in result["literal_fields"] if p["kind"] == "source_url"]
    assert len(urls) == 3 and all(p["source_text"].startswith("https://\neur") for p in urls)
    drafts, strategy = split_clauses([refs])
    assert strategy == "layout" and len(drafts) == 4
    assert all("2016" in u["source_text"] for u in result["translation_units"])


@pytest.mark.parametrize("mutation", ["amount", "url", "drop", "reorder", "span"])
def test_partition_corruption_cannot_be_hidden(mutation):
    prepared = prepared_table()
    if mutation == "amount":
        prepared["literal_fields"][1]["source_text"] = "$999"
    elif mutation == "url":
        prepared["literal_fields"].append(
            dict(kind="source_url", source_text="https://invented.example")
        )
    elif mutation == "drop":
        prepared["pieces"].pop()
    elif mutation == "reorder":
        prepared["literal_fields"].reverse()
    else:
        prepared["translation_units"][0]["source_spans"][0]["end"] += 1
    with pytest.raises(ValueError, match="partition"):
        source_repairs.verify_prepared(prepared)


def test_ordinal_date_is_protected_and_wrong_month_rejected_without_relaxing_density():
    original = "Made this 21st day of November 2013."
    assert dates(original) == {(2013, 11, 21)}
    _, mapping = translator._protect_explicit_dates(original)
    assert list(mapping.values()) == ["2013年11月21日"]
    assert translator.verify_translation(original, "于2013年11月21日制定。", [])[0]
    assert not translator.verify_translation(original, "于2013年12月21日制定。", [])[0]
    assert not translator.verify_translation("Amount\n1. 5,000 $100", "金额\n1. 5,000 $100", [])[0]


async def test_linguistic_units_keep_strict_checks_and_literal_copy_is_not_translation(monkeypatch):
    async def fake(source, *args, **kwargs):
        # Deliberately wrong signature date; still rejected after two attempts.
        text = (
            "于2013年12月21日制定，常任秘书，通信与新闻部，新加坡。"
            if "Made this" in source
            else (
                "及以上"
                if source == "and above"
                else "申请费用预付款，第一栏，第二栏，一次预付的新加坡电话号码数量，金额。10(3)14(1)"
            )
        )
        return text, "test", 10, 20, 1

    monkeypatch.setattr(translator, "_invoke_llm_translate", fake)
    prepared = prepared_table()
    result = await source_repairs.translate_prepared(prepared, [])
    assert not result["linguistic_units_passed"]
    assert result["review_status"] == "translation_failed"
    assert not result["whole_clause_translation_passed"] and not result["usable_as_legal_basis"]
    assert result["literal_integrity_passed"]
    assert result["units"][-1]["quality_issues"]
    assert len(result["units"][-1]["attempts"]) == 2
    assert all(field["rendered_text"] == field["source_text"] for field in result["literal_fields"])
    corrupt = copy.deepcopy(result)
    corrupt["literal_fields"][1]["rendered_text"] = "$999"
    with pytest.raises(ValueError, match="literal source"):
        source_repairs.verify_bilingual(prepared, corrupt)


async def test_append_repair_preserves_approved_sources_attempts_and_proposals(
    session_factory,
    monkeypatch,
    tmp_path,
):
    prepared = prepared_table()
    monkeypatch.setattr(source_repairs, "prepare_page", lambda *args: prepared)
    calls = 0

    async def fake(source, *args, **kwargs):
        nonlocal calls
        calls += 1
        if "Made this" in source:
            text = "于2013年11月21日制定，常任秘书，通信与新闻部，新加坡。"
        elif source == "and above":
            text = "及以上"
        elif "10(3)" in source:
            text = "第二附表，第10(3)条，申请费预付款，第一栏，第二栏，一次预付的新加坡电话号码数量，金额。"
        else:
            text = "第三附表，第14(1)条，终止号码的应付费用，第一栏，第二栏，终止的新加坡电话号码数量，费用。"
        return text, "test", 10, 20, 1

    monkeypatch.setattr(translator, "_invoke_llm_translate", fake)
    archive = ArchiveStore(tmp_path)
    blob = b"frozen original PDF bytes"
    digest, path = archive.put(blob)
    async with session_factory() as session:
        _, version, clauses = await make_document(session, ["7. 100,001 and above $3,200"])
        clause = clauses[0]
        clause.page_start = clause.page_end = 1
        version.content_hash, version.archive_path = digest, path
        edge, _, _ = await make_edge(session, clause, "approved")
        old = ClauseTranslation(
            clause_id=clause.id,
            translated_text="旧人工审批译文",
            engine="llm",
            model_name="original-approved-model",
            review_status="approved",
            quality_check_passed=True,
            is_current=True,
        )
        session.add(old)
        old_proposal = await propose(
            session, clause, {"old_source_proposal": "must remain pending"}
        )
        await session.commit()
        before = [record_payload(row) for row in (clause, version, edge, old, old_proposal)]
        source, proposal, bilingual, reused = await source_repairs.append_page_repair(
            session, archive, clause.id, 1, digest
        )
        assert not reused and bilingual.payload["linguistic_units_passed"]
        assert bilingual.payload["review_status"] == proposal.status == "pending_review"
        assert not source.payload["active"] and not bilingual.payload["human_reviewed"]
        assert source.payload["prepared"]["layout"]["text"] == TABLE
        assert source.content_hash == payload_hash(source.payload)
        assert bilingual.payload["source_revision_id"] == str(source.id)
        assert calls == 4
        again = await source_repairs.append_page_repair(session, archive, clause.id, 1, digest)
        assert again[2].id == bilingual.id and again[3] and calls == 4
        await session.commit()
        for row, expected in zip((clause, version, edge, old, old_proposal), before, strict=True):
            await session.refresh(row)
            assert record_payload(row) == expected
        assert archive.read(path) == blob
        assert await session.scalar(select(func.count()).select_from(KnowledgeAuditRecord)) == 2
        with pytest.raises(ValueError, match="identity differs"):
            await source_repairs.append_page_repair(
                session, archive, clause.id, 1, hashlib.sha256(b"wrong").hexdigest()
            )
