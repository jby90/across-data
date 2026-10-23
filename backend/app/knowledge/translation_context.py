"""Explicit, append-only translations of real source context for non-prose fragments.

The standalone fragment is never marked translated/approved. Context has its own source
spans, source hash, strict quality result and pending review status.
"""

from sqlalchemy import select

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_is_held
from app.knowledge import translator
from app.knowledge.history import payload_hash
from app.knowledge.models import ClauseTranslation, GlossaryTerm, KnowledgeAuditRecord


def is_numeric_fragment(text):
    return bool(text.strip()) and not any(char.isalpha() for char in text)


async def translate_fragment_context(session, clause, *, density_context=False):
    if not is_numeric_fragment(clause.original_text):
        current = await session.scalar(
            select(ClauseTranslation)
            .where(ClauseTranslation.clause_id == clause.id, ClauseTranslation.is_current.is_(True))
            .order_by(ClauseTranslation.updated_at.desc())
            .limit(1)
        )
        if not (
            density_context
            and current
            and current.review_status == "translation_failed"
            and current.quality_issues
            and all(issue.startswith("CJK 字符占比未达标") for issue in current.quality_issues)
        ):
            raise ValueError(
                "context requires an unchanged failed density check, never a semantic repair"
            )
    version = await session.get(DocumentVersion, clause.version_id)
    document = await session.get(Document, version.document_id)
    if await document_is_held(session, document.id):
        raise ValueError("held sources cannot be translated")
    sources = list(
        (
            await session.scalars(
                select(Clause)
                .where(Clause.version_id == clause.version_id, Clause.ordinal <= clause.ordinal)
                .order_by(Clause.ordinal.desc())
                .limit(12)
            )
        ).all()
    )
    index = next(
        (
            index
            for index, row in enumerate(sources)
            if (not density_context and sum(char.isalpha() for char in row.original_text) >= 40)
            or (
                density_context
                and index > 0
                and sum(
                    char.isalpha()
                    for prior in sources[1 : index + 1]
                    for char in prior.original_text
                )
                >= 200
            )
        ),
        None,
    )
    if index is None:
        raise ValueError("no adjacent prose within the same version; needs parse revision review")
    sources = list(reversed(sources[: index + 1]))
    spans, parts, position = [], [], 0
    for row in sources:
        if ArchiveStore.sha256(row.original_text.encode("utf-8")) != row.text_hash:
            raise ValueError("source text hash mismatch; context revision refused")
        parts.append(row.original_text)
        spans.append(
            dict(
                clause_id=str(row.id),
                text_hash=row.text_hash,
                start=position,
                end=position + len(row.original_text),
            )
        )
        position += len(row.original_text) + 1
    combined = "\n".join(parts)
    source_hash = payload_hash(dict(spans=spans, combined_source_text=combined))
    previous = await session.scalar(
        select(KnowledgeAuditRecord)
        .where(
            KnowledgeAuditRecord.target_id == clause.id,
            KnowledgeAuditRecord.action == "translation_context_revision",
            KnowledgeAuditRecord.payload["source_hash"].as_string() == source_hash,
            KnowledgeAuditRecord.payload["prompt_version"].as_string() == translator.PROMPT_VERSION,
            KnowledgeAuditRecord.payload["quality_check_passed"].as_boolean().is_(True),
        )
        .order_by(KnowledgeAuditRecord.created_at.desc())
        .limit(1)
    )
    if previous:
        return previous
    terms = list(
        (
            await session.scalars(
                select(GlossaryTerm).where(
                    (GlossaryTerm.country_code == document.country_code)
                    | (GlossaryTerm.country_code.is_(None))
                )
            )
        ).all()
    )
    effective = translator.select_effective_terms(terms, document.country_code, clause.language)
    all_hits, _ = translator.match_clause_glossary(combined, effective)
    translations, models, issues, tokens_in, tokens_out, duration = [], [], [], 0, 0, 0
    for chunk in translator.split_clause_into_chunks(combined):
        hits, unmatched = translator.match_clause_glossary(chunk, effective)
        result = None
        retry_issues = None
        for _attempt in range(2):
            result = await translator._invoke_llm_translate(
                chunk, clause.language, hits, unmatched, retry_issues=retry_issues
            )
            if result is None:
                issues.append("LLM unavailable; no context translation")
                break
            tokens_in += result[2]
            tokens_out += result[3]
            duration += result[4]
            passed, retry_issues, _ = translator.verify_translation(chunk, result[0], hits)
            if passed:
                break
        if result is not None:
            translations.append(result[0])
            models.append(result[1])
            issues.extend(retry_issues or [])
    translated = "\n".join(translations)
    passed, whole_issues, checked_hits = translator.verify_translation(
        combined, translated, all_hits
    )
    issues.extend(whole_issues)
    passed = passed and not issues
    payload = dict(
        primary_clause_id=str(clause.id),
        version_id=str(version.id),
        source_hash=source_hash,
        source_spans=spans,
        combined_source_text=combined,
        translated_text=translated,
        standalone_translation=False,
        standalone_fragment_unchanged=True,
        context_reason=(
            "failed_density_with_real_adjacent_source" if density_context else "numeric_fragment"
        ),
        quality_check_passed=passed,
        quality_issues=list(dict.fromkeys(issues)),
        review_status="pending_review" if passed else "translation_failed",
        usable_as_legal_basis=False,
        engine="llm",
        models=list(dict.fromkeys(models)),
        prompt_version=translator.PROMPT_VERSION,
        glossary_hits=checked_hits,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_ms=duration,
    )
    record = KnowledgeAuditRecord(
        target_type="clauses",
        target_id=clause.id,
        action="translation_context_revision",
        payload=payload,
        content_hash=payload_hash(payload),
    )
    session.add(record)
    await session.flush()
    return record
