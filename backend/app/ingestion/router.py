import base64
import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_platform_admin
from app.ingestion.archive import ArchiveIntegrityError, ArchiveStore
from app.ingestion.fetcher import validate_public_http_url
from app.ingestion.models import Clause, Document, DocumentVersion, LegalStatus, PipelineEvent
from app.ingestion.ocr import OcrEngine
from app.ingestion.pipeline import assess_document_relevance, parse_version
from app.sources.models import SourceSite

router = APIRouter(prefix="/api/admin", tags=["documents"])


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
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
    ocr_required: bool = False
    review_status: str = "pending_review"
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
    relevance: str = "relevant"
    relevance_note: str | None = None
    relevance_source: str = "rule"
    latest_version: VersionOut | None = None


class DocumentDetail(DocumentOut):
    versions: list[VersionOut]


class RelevanceOverrideIn(BaseModel):
    """管理员/法务人工覆盖文件级相关性(覆盖后自动流程不得改写)。"""

    relevance: Literal["relevant", "related", "out_of_scope"]
    note: str | None = Field(default=None, max_length=500)


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
    ocr_required: bool = False
    needs_review: bool = True


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: str
    status: str
    message: str | None
    created_at: datetime


class ManualUploadIn(BaseModel):
    """管理员手工上传官方原文(如源站开启了 WAF 验证码挑战时的合规兜底入口)。"""

    site_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    source_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    mime_type: str = Field(default="application/pdf", max_length=128)
    content_base64: str = Field(min_length=8)
    official_number: str | None = Field(default=None, max_length=128)
    version_label: str | None = Field(default=None, max_length=255)
    published_date: date | None = None
    effective_date: date | None = None


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


@router.post(
    "/documents/upload",
    response_model=DocumentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="手动上传官方原文(WAF 拦截时的兜底入口)",
)
async def upload_document_version(
    body: ManualUploadIn,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        raw_bytes = base64.b64decode(body.content_base64, validate=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"文件 base64 解码失败: {e}"
        ) from None
    settings = get_settings()
    if not raw_bytes or len(raw_bytes) > settings.crawl_max_bytes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "文件为空或超过体积上限")

    doc: Document | None = None
    if body.document_id is not None:
        doc = await session.get(Document, body.document_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    else:
        if body.site_id is None or not body.source_url:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "新建文档上传必须指定所属站点(site_id)与官方原文链接(source_url)",
            )
        site = await session.get(SourceSite, body.site_id)
        if site is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "站点不存在")
        try:
            clean_url = validate_public_http_url(body.source_url)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        doc = (
            await session.execute(
                select(Document).where(
                    Document.site_id == site.id, Document.source_url == clean_url
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            title_val = (body.title or clean_url).strip()[:500]
            rel_val, rel_note = assess_document_relevance(title_val, clean_url, site.document_type)
            doc = Document(
                site_id=site.id,
                country_code=site.country_code,
                source_url=clean_url,
                title=title_val,
                language=site.language,
                document_type=site.document_type,
                relevance=rel_val,
                relevance_note=rel_note,
            )
            session.add(doc)
            await session.flush()

    if body.title and body.title.strip():
        doc.title = body.title.strip()[:500]
    if not doc.relevance_note:
        doc.relevance, doc.relevance_note = assess_document_relevance(
            doc.title, doc.source_url, doc.document_type
        )
    doc.last_checked_at = datetime.now(UTC)
    doc.last_error = None

    archive = ArchiveStore(settings.archive_dir)
    digest = ArchiveStore.sha256(raw_bytes)
    existing = (
        await session.execute(
            select(DocumentVersion).where(
                DocumentVersion.document_id == doc.id,
                DocumentVersion.content_hash == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        stored_digest, rel = archive.put(raw_bytes)
        last_no = (
            await session.execute(
                select(DocumentVersion.version_no)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version = DocumentVersion(
            document_id=doc.id,
            version_no=(last_no or 0) + 1,
            content_hash=stored_digest,
            archive_path=rel,
            size_bytes=len(raw_bytes),
            mime_type=body.mime_type,
            final_url=doc.source_url[:2048],
            fetched_at=datetime.now(UTC),
            official_number=body.official_number,
            version_label=body.version_label,
            published_date=body.published_date,
            effective_date=body.effective_date,
            legal_status=LegalStatus.UNKNOWN.value,
            metadata_source="manual_upload",
        )
        session.add(version)
        await session.flush()
        session.add(
            PipelineEvent(
                version_id=version.id,
                step="upload",
                status="ok",
                message=f"管理员手动上传官方原文,{len(raw_bytes)} 字节,sha256={stored_digest[:12]}",
            )
        )
        ocr = OcrEngine(settings.tesseract_cmd, settings.tessdata_dir, settings.ocr_dpi)
        await parse_version(session, archive, version, doc.language, ocr)
        await session.commit()
    else:
        await session.commit()

    versions = (await _versions_by_doc(session, [doc.id])).get(doc.id, [])
    base = DocumentOut.model_validate(doc)
    version_out = [VersionOut.model_validate(v) for v in versions]
    return DocumentDetail(
        **base.model_dump(exclude={"latest_version"}),
        latest_version=version_out[0] if version_out else None,
        versions=version_out,
    )


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


@router.get("/documents/{document_id}/provenance", summary="只读来源与归档完整性核验")
async def document_provenance(
    document_id: uuid.UUID,
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    site = await session.get(SourceSite, doc.site_id)
    archive = ArchiveStore(get_settings().archive_dir)
    rows = []
    versions = (await _versions_by_doc(session, [doc.id])).get(doc.id, [])
    for version in versions:
        error = None
        try:
            raw = archive.read(version.archive_path)
            integrity_ok = (
                archive.sha256(raw) == version.content_hash and len(raw) == version.size_bytes
            )
            if not integrity_ok:
                error = "归档与登记的哈希或长度不一致"
        except (OSError, ArchiveIntegrityError) as exc:
            integrity_ok = False
            error = type(exc).__name__ + ": 归档不可读或完整性检查未通过"
        events = (
            await session.scalars(
                select(PipelineEvent)
                .where(PipelineEvent.version_id == version.id)
                .order_by(PipelineEvent.created_at, PipelineEvent.id)
            )
        ).all()
        steps = {event.step for event in events}
        # A URL and a matching hash prove provenance/integrity, not legal authenticity.
        acquisition = (
            "manual_upload"
            if "upload" in steps
            else ("pipeline_fetch" if "download" in steps else "legacy_or_unrecorded")
        )
        rows.append(
            {
                "version_id": str(version.id),
                "version_no": version.version_no,
                "content_hash": version.content_hash,
                "fetched_at": version.fetched_at,
                "final_url": version.final_url,
                "metadata_source": version.metadata_source,
                "acquisition": acquisition,
                "pipeline_refetch_verified": "refetch" in steps,
                "integrity_ok": integrity_ok,
                "integrity_error": error,
                "review_status": version.review_status,
                "legal_status": version.legal_status,
                "effective_date": version.effective_date,
                "ocr_required": version.ocr_required,
                "official_authenticity": "pending_human_review",
                "events": [EventOut.model_validate(event).model_dump() for event in events],
            }
        )
    return {
        "document_id": str(doc.id),
        "country_code": doc.country_code,
        "source_url": doc.source_url,
        "language": doc.language,
        "publisher": site.publisher if site else None,
        "terms_status": site.terms_status if site else None,
        "terms_approval_scope": site.terms_approval_scope if site else None,
        "versions": rows,
    }


@router.patch(
    "/documents/{document_id}/relevance",
    response_model=DocumentOut,
    summary="人工覆盖文件级相关性(核心/相关/范围外)",
)
async def override_document_relevance(
    document_id: uuid.UUID,
    body: RelevanceOverrideIn,
    principal: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    doc = await session.get(Document, document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文档不存在")
    doc.relevance = body.relevance
    doc.relevance_source = "manual"
    doc.relevance_note = (body.note or "").strip() or "管理员人工覆盖相关性判定"
    await session.commit()
    await session.refresh(doc)
    return DocumentOut.model_validate(doc)


async def _version_or_404(session: AsyncSession, version_id: uuid.UUID) -> DocumentVersion:
    v = await session.get(DocumentVersion, version_id)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "版本不存在")
    return v


@router.get("/versions/{version_id}/original", summary="下载经完整性核验的归档原文")
async def download_original(
    version_id: uuid.UUID,
    _: Principal = Depends(require_admin_side),
    session: AsyncSession = Depends(get_session),
):
    version = await _version_or_404(session, version_id)
    try:
        archive = ArchiveStore(get_settings().archive_dir)
        raw = archive.read(version.archive_path)
    except (OSError, ArchiveIntegrityError):
        raise HTTPException(409, "归档原文不存在或完整性异常，已拒绝下载") from None
    if archive.sha256(raw) != version.content_hash or len(raw) != version.size_bytes:
        raise HTTPException(409, "归档与登记的内容哈希或长度不一致，已拒绝下载")
    extension = ".pdf" if version.mime_type == "application/pdf" else ".bin"
    return Response(
        raw,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{version.id}{extension}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


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
