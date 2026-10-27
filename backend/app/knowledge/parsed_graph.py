"""Proposed graph evidence from immutable page repairs, isolated from active legal edges.

Reuse the existing extractor, type/quote gates and independent judge. No new graph
platform, no source rewriting, and no approval inheritance. References and signatures
are explicitly non-normative. Empty results remain empty, not invented obligations.
"""

from sqlalchemy import select

from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.safety import document_is_held
from app.knowledge import extractor
from app.knowledge.history import payload_hash
from app.knowledge.models import KnowledgeAuditRecord, NodeType, RelationType
from app.knowledge.source_repairs import prepare_page, verify_prepared
from app.sources.models import SourceSite

CONTRACT = "typed-page-graph-v1"


def validate_cached_result(payload, source, prepared):
    """Recheck every cached relation against its exact immutable page span."""
    if (
        payload["source_revision_id"] != str(source.id)
        or payload["source_hash"] != source.content_hash
    ):
        raise ValueError("source revision/hash differs")
    if payload["active"] or payload["usable_as_legal_basis"] or payload["human_reviewed"]:
        raise ValueError("pending page graph cannot inherit legal approval")
    for number, block in enumerate(payload["blocks"]):
        if block["block_number"] != number:
            raise ValueError("graph source blocks duplicated or reordered")
        original = prepared["blocks"][block["block_number"]]
        span = {k: original[k] for k in ("start", "end")}
        text = prepared["layout"]["text"][span["start"] : span["end"]]
        if (
            block["source_span"] != span
            or block["source_text"] != text
            or block["kind"] != original["kind"]
        ):
            raise ValueError("graph block source differs")
        if original["kind"] != "table" and block["relations"]:
            raise ValueError("non-normative block cannot contain legal relations")
        if original["kind"] == "table" and block["status"] == "completed":
            extractor.ClauseExtractionPayload.model_validate(block["extraction_payload"])
        for row in block["relations"]:
            evaluation = extractor.CandidateEvaluationResult.model_validate(row["evaluation"])
            relation = extractor.ExtractedRelation.model_validate(row["relation"])
            if not evaluation.admitted:
                continue
            if relation.type == RelationType.PENALIZES:
                raise ValueError("fee schedule cannot establish penalties")
            quote = evaluation.aligned_quote
            if not (
                quote
                and quote in text
                and evaluation.judge_verdict == "yes"
                and evaluation.judge_model
            ):
                raise ValueError("cached relation lacks verified independent evidence")
            ok, _ = extractor.validate_relation_type_constraints(
                relation.type,
                row["source_type"],
                row["target_type"],
                quote,
                row["source_name"],
                row["target_name"],
                payload["document_title"],
            )
            if not ok or text[row["quote_span"]["start"] : row["quote_span"]["end"]] != quote:
                raise ValueError("cached relation type/quote span differs")
    if len(payload["blocks"]) != len(prepared["blocks"]):
        raise ValueError("graph source block omitted")


async def propose_parsed_graph(session, source, archive):
    p = source.payload
    if source.action != "source_parse_revision" or payload_hash(p) != source.content_hash:
        raise ValueError("immutable source revision checksum differs")
    if (
        p["active"]
        or p["human_reviewed"]
        or p["usable_as_legal_basis"]
        or p["review_status"] != "pending_review"
    ):
        raise ValueError("this path only accepts unactivated pending page repairs")
    clause = await session.get(Clause, source.target_id)
    version = await session.get(DocumentVersion, clause.version_id)
    doc = await session.get(Document, version.document_id)
    if await document_is_held(session, doc.id):
        raise ValueError("held document cannot supply proposed legal evidence")
    blob = archive.read(version.archive_path)
    if (
        p["version_id"] != str(version.id)
        or p["pdf_content_hash"] != version.content_hash
        or ArchiveStore.sha256(blob) != p["pdf_content_hash"]
        or clause.text_hash != p["original_clause_text_hash"]
        or clause.original_text != p["original_clause_text"]
        or ArchiveStore.sha256(clause.original_text.encode()) != clause.text_hash
    ):
        raise ValueError("original PDF/clause binding differs")
    prepared = p["prepared"]
    verify_prepared(prepared)
    import asyncio

    if await asyncio.to_thread(prepare_page, blob, p["page_number"]) != prepared:
        raise ValueError("fresh PDF reading differs from pending source repair")
    settings = get_settings()
    key = payload_hash(
        dict(
            contract=CONTRACT,
            source_hash=source.content_hash,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            thinking=settings.llm_kg_extraction_thinking,
            extractor=extractor.EXTRACTOR_PROMPT_VERSION,
            judge=extractor.JUDGE_PROMPT_VERSION,
        )
    )
    for cached in (
        await session.scalars(
            select(KnowledgeAuditRecord)
            .where(
                KnowledgeAuditRecord.action == "kg_parsed_source_result",
                KnowledgeAuditRecord.target_id == clause.id,
                KnowledgeAuditRecord.payload["cache_key"].as_string() == key,
            )
            .order_by(KnowledgeAuditRecord.created_at.desc())
        )
    ).all():
        if (
            payload_hash(cached.payload) != cached.content_hash
            or cached.payload.get("status") != "completed"
        ):
            continue
        try:
            validate_cached_result(cached.payload, source, prepared)
        except (ValueError, KeyError, IndexError):
            continue
        return cached, True
    site = await session.get(SourceSite, doc.site_id)
    publisher = site.publisher if site else ""
    blocks, failed = [], False
    for number, block in enumerate(prepared["blocks"]):
        span = {k: block[k] for k in ("start", "end")}
        text = prepared["layout"]["text"][span["start"] : span["end"]]
        result = dict(
            block_number=number,
            kind=block["kind"],
            source_span=span,
            source_text=text,
            relations=[],
            review_status="pending_review",
        )
        if block["kind"] != "table":
            result.update(
                status="excluded_non_normative",
                reason="reference/signature/page identifiers are not legal duties",
            )
            blocks.append(result)
            continue
        extracted = await extractor._call_llm_extract_clause(
            clause_text=text,
            clause_number=None,
            document_title=doc.title,
            publisher_name=publisher,
            country_code=doc.country_code,
        )
        if extracted is None:
            failed = True
            result.update(status="failed", reason="model_unavailable_or_invalid_schema")
            blocks.append(result)
            continue
        data, model = extracted
        result.update(
            status="completed",
            extraction_payload=data.model_dump(mode="json"),
            extractor_model=model,
        )
        entities = {
            label.strip().casefold(): ent
            for ent in data.entities
            for label in (ent.name_zh, ent.name_orig)
            if label
        }
        for rel in data.relations:

            def endpoint(name, lookup=entities):
                if name.strip().casefold() == doc.title.strip().casefold() or name in (
                    "本法",
                    "本法规",
                    "本条例",
                    "本指南",
                ):
                    return NodeType.INSTRUMENT, doc.title
                if publisher and name.strip().casefold() == publisher.strip().casefold():
                    return NodeType.REGULATOR, publisher
                if ent := lookup.get(name.strip().casefold()):
                    return ent.type, ent.name_zh
                return None

            src, dst = endpoint(rel.source), endpoint(rel.target)
            if not src or not dst:
                result["relations"].append(
                    dict(
                        relation=rel.model_dump(mode="json"),
                        evaluation=dict(
                            admitted=False,
                            stage="unresolved_endpoint",
                            aligned_quote=None,
                            confidence=0,
                            judge_verdict="no",
                            judge_reason="never guess an unresolved entity",
                        ),
                    )
                )
                continue
            if rel.type == RelationType.PENALIZES:
                # These typed blocks are fee schedules, not sanctions. Preserve the
                # rejected candidate; monetary cells cannot establish a legal penalty.
                evaluation = extractor.CandidateEvaluationResult(
                    admitted=False,
                    stage="fee_is_not_penalty",
                    aligned_quote=None,
                    confidence=0,
                    judge_verdict="no",
                    judge_reason="fee schedule amounts do not establish penalties",
                )
            else:
                evaluation = await extractor.evaluate_kg_candidate(
                    relation_type=rel.type,
                    source_type=src[0],
                    source_name=src[1],
                    target_type=dst[0],
                    target_name=dst[1],
                    evidence_quote=rel.evidence_quote,
                    clause_original_text=text,
                    document_title=doc.title,
                    confidence=rel.confidence,
                    use_llm_judge=True,
                )
            if evaluation.stage == "semantic_judge_unavailable":
                failed = True
            quote = evaluation.aligned_quote or ""
            start = text.find(quote) if quote else -1
            result["relations"].append(
                dict(
                    relation=rel.model_dump(mode="json"),
                    evaluation=evaluation.model_dump(),
                    source_type=src[0].value,
                    source_name=src[1],
                    target_type=dst[0].value,
                    target_name=dst[1],
                    quote_span=dict(start=start, end=start + len(quote)),
                )
            )
        blocks.append(result)
    payload = dict(
        contract=CONTRACT,
        cache_key=key,
        source_revision_id=str(source.id),
        source_hash=source.content_hash,
        parent_clause_id=str(clause.id),
        version_id=str(version.id),
        pdf_content_hash=version.content_hash,
        page_number=p["page_number"],
        country_code=doc.country_code,
        document_title=doc.title,
        blocks=blocks,
        status="failed" if failed else "completed",
        review_status="pending_review",
        active=False,
        usable_as_legal_basis=False,
        human_reviewed=False,
        extractor_prompt=extractor.EXTRACTOR_PROMPT_VERSION,
        judge_prompt=extractor.JUDGE_PROMPT_VERSION,
    )
    validate_cached_result(payload, source, prepared)
    record = KnowledgeAuditRecord(
        target_type="clauses",
        target_id=clause.id,
        action="kg_parsed_source_result",
        payload=payload,
        content_hash=payload_hash(payload),
    )
    session.add(record)
    await session.flush()
    return record, False


async def pending_parsed_sources(session, country_code=None):
    query = (
        select(KnowledgeAuditRecord)
        .join(Clause, Clause.id == KnowledgeAuditRecord.target_id)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(KnowledgeAuditRecord.action == "source_parse_revision")
        .order_by(KnowledgeAuditRecord.created_at.desc())
    )
    if country_code:
        query = query.where(Document.country_code == country_code)
    latest = {}
    for row in (await session.scalars(query)).all():
        latest.setdefault(row.target_id, row)
    return [row for row in latest.values() if "prepared" in row.payload]
