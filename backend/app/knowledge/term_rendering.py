"""Append Chinese reading aids to exact source definitions; never approve legal content."""

import uuid

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.ingestion.models import Document
from app.knowledge.legal_terms import CATALOG, current_definitions, sha
from app.knowledge.models import LegalTermDefinition
from app.knowledge.translator import verify_translation

RENDER_VERSION = "definition-zh-v1"


class Rendering(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition_id: uuid.UUID
    target_zh: str = Field(min_length=1, max_length=120)
    definition_zh: str = Field(min_length=8, max_length=12000)


class RenderingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definitions: list[Rendering] = Field(max_length=10)


async def append_rendering(session, source, rendering):
    if rendering.definition_id != source.id:
        raise ValueError("释义不属于所给来源")
    fingerprint = sha(
        f"{RENDER_VERSION}:{source.fingerprint}:{rendering.target_zh}:{rendering.definition_zh}"
    )
    doc = await session.get(Document, source.document_id)
    eligible = await current_definitions(
        session, source.country_code or doc.country_code, source.document_id
    )
    if not any(t.id == source.id or t.fingerprint == fingerprint for t in eligible):
        raise ValueError("来源已变化或已不可用，不写入中文释义")
    prescribed = next((target for code, target, _ in CATALOG if code == source.concept_code), None)
    if source.registration_origin in {"manual", "machine_reference"}:
        prescribed = source.target_zh
    if prescribed and rendering.target_zh != prescribed:
        raise ValueError("中文译名不符合术语库规范")
    hit = dict(
        source_term=source.source_term,
        target_zh=rendering.target_zh,
        concept_code=source.concept_code,
    )
    passed, issues, _ = verify_translation(
        source.definition_original, rendering.definition_zh, [hit]
    )
    if not passed:
        raise ValueError("；".join(issues))
    copy_fields = (
        "concept_code",
        "country_code",
        "language",
        "source_term",
        "definition_original",
        "source_kind",
        "source_url",
        "source_reference",
        "document_id",
        "version_id",
        "clause_id",
        "quote_start",
        "quote_end",
        "source_text_sha256",
        "applies_to_document_id",
    )
    await session.execute(
        insert(LegalTermDefinition)
        .values(
            **{key: getattr(source, key) for key in copy_fields},
            id=uuid.uuid4(),
            target_zh=rendering.target_zh,
            definition_zh=rendering.definition_zh,
            fingerprint=fingerprint,
            verification_status="source_matched",
            registration_origin=(
                "machine_reference"
                if source.registration_origin in {"manual", "machine_reference"}
                else "machine_translation"
            ),
        )
        .on_conflict_do_nothing(index_elements=["fingerprint"])
    )
    return await session.scalar(
        select(LegalTermDefinition).where(LegalTermDefinition.fingerprint == fingerprint)
    )
