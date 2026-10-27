"""Append-only page parsing and bilingual revisions. Never rewrite existing legal data.

Numerical cells, names, IDs and URLs are source fields, not translations. Every
linguistic unit uses the normal chunk and whole-unit gates, without density padding.
Neither kind of check grants legal approval or changes the original failed row.
"""

import asyncio
import uuid

import pypdfium2 as pdfium
from sqlalchemy import select

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.parsing import _PDFIUM_LOCK
from app.ingestion.safety import document_is_held
from app.ingestion.source_layout import ltr_page, partition_page, resolve_unmapped_hyphens
from app.knowledge import translator
from app.knowledge.history import payload_hash, propose
from app.knowledge.models import GlossaryTerm, KnowledgeAuditRecord


def prepare_page(blob, page_number):
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(blob)
        try:
            page = pdf[page_number - 1]
            text_page = page.get_textpage()
            try:
                layout = resolve_unmapped_hyphens(page, text_page, ltr_page(text_page))
                return {"layout": layout, **partition_page(layout["text"])}
            finally:
                text_page.close()
                page.close()
        finally:
            pdf.close()


def verify_prepared(prepared):
    """Rebuild the partition, so missing/reordered rows, URLs or spans fail closed."""
    expected = partition_page(prepared["layout"]["text"])
    for field in ("blocks", "pieces", "translation_units", "literal_fields"):
        if prepared[field] != expected[field]:
            raise ValueError(f"source partition {field} differs; refused")
    if not prepared["layout"]["glyph_coverage_passed"]:
        raise ValueError("PDF glyph coverage must pass")
    return True


async def translate_prepared(prepared, effective_terms):
    verify_prepared(prepared)
    results, totals = [], dict(tokens_in=0, tokens_out=0, duration_ms=0)
    for unit_no, unit in enumerate(prepared["translation_units"]):
        source = unit["source_text"]
        all_hits, _ = translator.match_clause_glossary(source, effective_terms)
        chunks, issues, attempts = [], [], []
        for chunk in translator.split_clause_into_chunks(source):
            hits, unmatched = translator.match_clause_glossary(chunk, effective_terms)
            retry_issues = None
            result = None
            for attempt in range(2):
                result = await translator._invoke_llm_translate(
                    chunk, unit["language"], hits, unmatched, retry_issues=retry_issues
                )
                if result is None:
                    retry_issues = ["LLM unavailable; source retained"]
                    attempts.append(dict(attempt=attempt + 1, passed=False, issues=retry_issues))
                    break
                for key, value in zip(totals, result[2:], strict=True):
                    totals[key] += value
                passed, retry_issues, _ = translator.verify_translation(chunk, result[0], hits)
                attempts.append(
                    dict(
                        attempt=attempt + 1,
                        passed=passed,
                        issues=retry_issues,
                        source_text=chunk,
                        translated_text=result[0],
                        model=result[1],
                        tokens_in=result[2],
                        tokens_out=result[3],
                        duration_ms=result[4],
                    )
                )
                if passed:
                    break
            chunks.append(result[0] if result else "")
            issues.extend(retry_issues or [])
        translated = "\n".join(chunks)
        passed, whole_issues, checked_hits = translator.verify_translation(
            source, translated, all_hits
        )
        issues.extend(whole_issues)
        results.append(
            {
                **unit,
                "unit_number": unit_no,
                "translated_text": translated,
                "quality_check_passed": passed and not issues,
                "quality_issues": list(dict.fromkeys(issues)),
                "attempts": attempts,
                "glossary_hits": checked_hits,
            }
        )
    literals = [
        {
            **field,
            "rendered_text": field["source_text"],
            "source_sha256": ArchiveStore.sha256(field["source_text"].encode()),
            "mode": "source_literal_copy_not_translation",
        }
        for field in prepared["literal_fields"]
    ]
    result = dict(
        units=results,
        literal_fields=literals,
        literal_integrity_passed=True,
        linguistic_units_passed=bool(results) and all(r["quality_check_passed"] for r in results),
        whole_clause_translation_passed=False,
        standalone_translation=False,
        review_status="pending_review"
        if all(r["quality_check_passed"] for r in results)
        else "translation_failed",
        active=False,
        usable_as_legal_basis=False,
        human_reviewed=False,
        prompt_version=translator.PROMPT_VERSION,
        **totals,
    )
    verify_bilingual(prepared, result)
    return result


def verify_bilingual(prepared, bilingual):
    verify_prepared(prepared)
    if len(bilingual["units"]) != len(prepared["translation_units"]):
        raise ValueError("translation unit omitted or duplicated")
    for source, translated in zip(prepared["translation_units"], bilingual["units"], strict=True):
        if any(translated[key] != value for key, value in source.items()):
            raise ValueError("translation attached to a different source span")
    if len(bilingual["literal_fields"]) != len(prepared["literal_fields"]):
        raise ValueError("literal field omitted or duplicated")
    for source, literal in zip(
        prepared["literal_fields"], bilingual["literal_fields"], strict=True
    ):
        if (
            any(literal[key] != value for key, value in source.items())
            or literal["rendered_text"] != source["source_text"]
        ):
            raise ValueError("literal source data changed or reassigned")
        if literal["source_sha256"] != ArchiveStore.sha256(source["source_text"].encode()):
            raise ValueError("literal source hash differs")
    if (
        bilingual["whole_clause_translation_passed"]
        or bilingual["active"]
        or bilingual["usable_as_legal_basis"]
        or bilingual["human_reviewed"]
    ):
        raise ValueError("typed machine revision cannot claim whole-clause or legal approval")
    return True


async def append_page_repair(session, archive, clause_id, page_number, expected_pdf_hash):
    clause = await session.get(Clause, uuid.UUID(str(clause_id)))
    if clause is None:
        raise ValueError("unknown source clause")
    version = await session.get(DocumentVersion, clause.version_id)
    document = await session.get(Document, version.document_id)
    if await document_is_held(session, document.id):
        raise ValueError("held source cannot be repaired as valid evidence")
    if (
        clause.page_start is None
        or clause.page_end is None
        or not clause.page_start <= page_number <= clause.page_end
    ):
        raise ValueError("repair page must intersect the original clause")
    blob = archive.read(version.archive_path)
    if (
        ArchiveStore.sha256(blob) != expected_pdf_hash
        or version.content_hash != expected_pdf_hash
        or ArchiveStore.sha256(clause.original_text.encode()) != clause.text_hash
    ):
        raise ValueError("frozen PDF or original clause identity differs")
    prepared = await asyncio.to_thread(prepare_page, blob, page_number)
    verify_prepared(prepared)
    source_payload = dict(
        primary_clause_id=str(clause.id),
        version_id=str(version.id),
        page_number=page_number,
        pdf_content_hash=expected_pdf_hash,
        original_clause_text=clause.original_text,
        original_clause_text_hash=clause.text_hash,
        parser_engine=prepared["layout"]["engine"],
        prepared=prepared,
        content_kind="typed_table_signature"
        if any(b["kind"] == "table" for b in prepared["blocks"])
        else "typed_bibliography",
        scope="whole_source_page",
        review_status="pending_review",
        active=False,
        usable_as_legal_basis=False,
        human_reviewed=False,
        standalone_translation=False,
    )
    source_hash = payload_hash(source_payload)
    record = await session.scalar(
        select(KnowledgeAuditRecord).where(
            KnowledgeAuditRecord.target_id == clause.id,
            KnowledgeAuditRecord.action == "source_parse_revision",
            KnowledgeAuditRecord.content_hash == source_hash,
        )
    )
    if record is None:
        record = KnowledgeAuditRecord(
            target_type="clauses",
            target_id=clause.id,
            action="source_parse_revision",
            payload=source_payload,
            content_hash=source_hash,
        )
        session.add(record)
        await session.flush()
    proposal = await propose(session, clause, {"source_parse_revision": source_payload})
    await session.flush()
    terms = list(
        (
            await session.scalars(
                select(GlossaryTerm).where(
                    (GlossaryTerm.country_code == document.country_code)
                    | GlossaryTerm.country_code.is_(None)
                )
            )
        ).all()
    )
    effective = translator.select_effective_terms(terms, document.country_code, "en")
    # Freeze all effective glossary values; a changed glossary must not reuse old checks.
    glossary_hash = payload_hash(
        {"terms": sorted((str(t.id), t.source_term, t.target_zh) for t in effective)}
    )
    prior = await session.scalar(
        select(KnowledgeAuditRecord)
        .where(
            KnowledgeAuditRecord.target_id == clause.id,
            KnowledgeAuditRecord.action == "parsed_source_translation",
            KnowledgeAuditRecord.payload["source_hash"].as_string() == source_hash,
            KnowledgeAuditRecord.payload["glossary_hash"].as_string() == glossary_hash,
            KnowledgeAuditRecord.payload["prompt_version"].as_string() == translator.PROMPT_VERSION,
            KnowledgeAuditRecord.payload["linguistic_units_passed"].as_boolean().is_(True),
        )
        .order_by(KnowledgeAuditRecord.created_at.desc())
        .limit(1)
    )
    if prior:
        return record, proposal, prior, True
    result = await translate_prepared(prepared, effective)
    payload = dict(
        source_hash=source_hash,
        source_revision_id=str(record.id),
        source_proposal_id=str(proposal.id),
        glossary_hash=glossary_hash,
        page_number=page_number,
        pdf_content_hash=expected_pdf_hash,
        **result,
    )
    bilingual = KnowledgeAuditRecord(
        target_type="clauses",
        target_id=clause.id,
        action="parsed_source_translation",
        payload=payload,
        content_hash=payload_hash(payload),
    )
    session.add(bilingual)
    await session.flush()
    return record, proposal, bilingual, False
