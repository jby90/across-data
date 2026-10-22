import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side
from app.ingestion.models import Clause, Document, DocumentVersion, PipelineEvent

router = APIRouter(prefix="/api/admin", tags=["documents"])


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_no: int
    content_hash: str
    size_bytes: int
    mime_type: str
    fetched_at: datetime
    state: str
    page_count: int | None
    char_count: int | None
    parse_strategy: str | None
    error: str | None
    text_source: str
    ocr_confidence: float | None
    official_number: str | None
    version_label: str | None
    published_date: date | None
    effective_date: date | None
    repealed_date: date | None
    legal_status: str
    metadata_source: str | None


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    site_id: uuid.UUID
    country_code: str
    source_url: str
    title: str
    language: str
    document_type: str
    discovered_at: datetime
    last_checked_at: datetime | None
    last_error: str | None
    instrument_family: str | None = None
    instrument_kind: str | None = None
    latest_version: VersionOut | None = None


class DocumentDetail(DocumentOut):
    versions: list[VersionOut]


class ClauseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    clause_number: str | None
    heading: str | None
    section_path: str | None
    original_text: str
    page_start: int | None
    page_end: int | None
    language: str


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: str
    status: str
    message: str | None
    created_at: datetime


async def _versions_by_doc(
    session: AsyncSession, doc_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[DocumentVersion]]:
    if not doc_ids:
        return {}
    rows = (
        await session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id.in_(doc_ids))
            .order_by(DocumentVersion.document_id, DocumentVersion.version_no.desc())
        )
    ).scalars()
    grouped: dict[uuid.UUID, list[DocumentVersion]] = {}
    for v in rows:
        grouped.setdefault(v.document_id, []).append(v)
    return grouped


@router.get("/documents", response_model=list[DocumentOut], summary="文档库列表")
async def list_documents(
    country_code: str | None = None,
    site_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Document).order_by(Document.discovered_at.desc(), Document.id)
    if country_code:
        stmt = stmt.where(Document.country_code == country_code.upper())
    if site_id:
        stmt = stmt.where(Document.site_id == site_id)
    docs = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    versions = await _versions_by_doc(session, [d.id for d in docs])
    out = []
    for d in docs:
        item = DocumentOut.model_validate(d)
        vs = versions.get(d.id)
        item.latest_version = VersionOut.model_validate(vs[0]) if vs else None
        out.append(item)
    return out


@router.get("/documents/{document_id}", response_model=DocumentDetail, summary="文档详情与全部版本")
async def get_document(
    document_id: uuid.UUID,
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    versions = (await _versions_by_doc(session, [doc.id])).get(doc.id, [])
    base = DocumentOut.model_validate(doc)
    version_out = [VersionOut.model_validate(v) for v in versions]
    return DocumentDetail(
        **base.model_dump(exclude={"latest_version"}),
        latest_version=version_out[0] if version_out else None,
        versions=version_out,
    )


async def _version_or_404(session: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
    v = await session.get(DocumentVersion, version_id)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    return v


@router.get("/versions/{version_id}/clauses", response_model=list[ClauseOut], summary="版本条款")
async def list_clauses(
    version_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
):
    await _version_or_404(session, version_id)
    stmt = (
        select(Clause)
        .where(Clause.version_id == version_id)
        .order_by(Clause.ordinal)
        .limit(limit)
        .offset(offset)
    )
    return (await session.execute(stmt)).scalars().all()


@router.get("/versions/{version_id}/events", response_model=list[EventOut], summary="流水线事件")
async def list_events(
    version_id: uuid.UUID,
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
):
    await _version_or_404(session, version_id)
    stmt = (
        select(PipelineEvent)
        .where(PipelineEvent.version_id == version_id)
        .order_by(PipelineEvent.id)
    )
    return (await session.execute(stmt)).scalars().all()
