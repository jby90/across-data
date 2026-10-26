"""Legal terminology endpoints: source-derived reading aids and exceptional ambiguities."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_user_side
from app.knowledge.legal_terms import (
    CATALOG,
    current_definitions,
    rebuild_library,
    register_critical_ambiguity,
    register_reference,
    term_payload,
)
from app.knowledge.models import ReviewPoint

admin = APIRouter(
    prefix="/api/admin/knowledge/terminology",
    tags=["terminology-admin"],
    dependencies=[Depends(require_admin_side)],
)
user = APIRouter(
    prefix="/api/knowledge/terminology",
    tags=["terminology"],
    dependencies=[Depends(require_user_side)],
)
Session = Annotated[AsyncSession, Depends(get_session)]
Admin = Annotated[Principal, Depends(require_admin_side)]
User = Annotated[Principal, Depends(require_user_side)]


@user.get("")
@admin.get("")
async def definitions(
    session: Session,
    country_code: str = Query(pattern="^[A-Za-z]{2}$"),
    document_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    q: str = Query(default="", max_length=150),
):
    rows = await current_definitions(session, country_code, document_id, version_id)
    from app.knowledge.legal_terms import PRIORITY

    rows.sort(key=lambda t: (PRIORITY[t.source_kind], t.source_term.casefold(), str(t.id)))
    items = [term_payload(t) for t in rows]
    present = {t.concept_code for t in rows}
    for code, target, aliases in CATALOG:
        if code in present:
            continue
        items.append(
            dict(
                id=code,
                concept_code=code,
                country_code=None,
                language="multi",
                source_term=" / ".join(aliases.get("en", [])),
                target_zh=target,
                aliases=[a for lang in aliases.values() for a in lang],
                definition_original=None,
                definition_zh=None,
                source_url=None,
                source_reference="通用译名",
                source_kind="terminology_only",
                source_label="通用译名",
                document_id=None,
                version_id=None,
                verification_status="lexical_only",
            )
        )
    if q.strip():
        items = [
            t
            for t in items
            if q.strip().casefold() in " ".join(str(v or "") for v in t.values()).casefold()
        ]
    return {"items": items, "total": len(items)}


class RebuildIn(BaseModel):
    country_code: str | None = Field(default=None, pattern="^[A-Za-z]{2}$")
    document_id: uuid.UUID | None = None


@admin.post("/rebuild")
async def rebuild(body: RebuildIn, _: Admin, session: Session):
    result = await rebuild_library(session, body.country_code, body.document_id)
    await session.commit()
    return result


class ReferenceIn(BaseModel):
    clause_id: uuid.UUID
    source_term: str = Field(min_length=2, max_length=100)
    target_zh: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=12, max_length=6000)
    source_kind: Literal[
        "law_definition", "regulator_guidance", "official_parallel", "international_reference"
    ]
    country_code: str | None = Field(default=None, pattern="^[A-Za-z]{2}$")
    applies_to_document_id: uuid.UUID | None = None


@admin.post("/references")
async def reference(body: ReferenceIn, admin_user: Admin, session: Session):
    try:
        row = await register_reference(session, **body.model_dump(), actor_id=admin_user.user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return term_payload(row)


class AmbiguityIn(BaseModel):
    clause_id: uuid.UUID
    source_term: str = Field(min_length=2, max_length=100)
    question: str = Field(min_length=5, max_length=1500)
    impact: str = Field(min_length=5, max_length=1500)


@admin.post("/ambiguities")
async def ambiguity(body: AmbiguityIn, _: Admin, session: Session):
    try:
        row = await register_critical_ambiguity(session, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return {"id": str(row.id), "status": row.status}


@admin.get("/ambiguities")
async def ambiguities(_: Admin, session: Session):
    rows = (
        await session.scalars(
            select(ReviewPoint)
            .where(ReviewPoint.point_code.like("TERM-%"))
            .order_by(ReviewPoint.created_at.desc())
        )
    ).all()
    return [
        dict(
            id=str(r.id),
            country_code=r.country_code,
            title=r.title,
            question=r.question,
            impact=r.impact_on_conclusion,
            status=r.status,
        )
        for r in rows
    ]
