import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.iam.deps import Principal, require_platform_admin
from app.ingestion.models import Document
from app.sources.models import CrawlRun, SourceSite, TermsStatus
from app.sources.schemas import RunOut, SiteCreate, SiteOut, SitePatch, TermsDecision

router = APIRouter(prefix="/api/admin/sites", tags=["sources"])


async def _get_site(session: AsyncSession, site_id: uuid.UUID) -> SourceSite:
    site = await session.get(SourceSite, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "站点不存在")
    return site


async def _attach_doc_summaries(session: AsyncSession, sites: list[SourceSite]) -> list[SiteOut]:
    if not sites:
        return []
    ids = [s.id for s in sites]
    rows = (
        await session.execute(
            select(Document.site_id, Document.title)
            .where(Document.site_id.in_(ids))
            .order_by(Document.discovered_at.desc(), Document.id)
        )
    ).all()
    counts: dict[uuid.UUID, int] = {}
    titles: dict[uuid.UUID, list[str]] = {}
    for sid, title in rows:
        counts[sid] = counts.get(sid, 0) + 1
        bucket = titles.setdefault(sid, [])
        if len(bucket) < 3:
            bucket.append(title)
    out: list[SiteOut] = []
    for s in sites:
        item = SiteOut.model_validate(s)
        item.document_count = counts.get(s.id, 0)
        item.sample_titles = titles.get(s.id, [])
        out.append(item)
    return out


@router.get("", response_model=list[SiteOut], summary="站点列表")
async def list_sites(
    country_code: str | None = None,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(SourceSite).order_by(
        SourceSite.country_code, SourceSite.publisher, SourceSite.entry_url
    )
    if country_code:
        stmt = stmt.where(SourceSite.country_code == country_code.upper())
    sites = list((await session.execute(stmt)).scalars().all())
    return await _attach_doc_summaries(session, sites)


@router.post("", response_model=SiteOut, status_code=201, summary="新增站点(默认停用、条款待确认)")
async def create_site(
    body: SiteCreate,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    site = SourceSite(
        country_code=body.country_code,
        publisher=body.publisher.strip(),
        language=body.language,
        document_type=body.document_type,
        entry_url=body.entry_url,
        strategy=body.strategy.value,
        config=body.config.model_dump(),
        interval_hours=body.interval_hours,
        terms_url=body.terms_url,
    )
    session.add(site)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该国家下已存在相同入口地址的站点") from None
    return (await _attach_doc_summaries(session, [site]))[0]


@router.get("/{site_id}", response_model=SiteOut, summary="站点详情")
async def get_site(
    site_id: uuid.UUID,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await _get_site(session, site_id)
    return (await _attach_doc_summaries(session, [site]))[0]


@router.patch("/{site_id}", response_model=SiteOut, summary="修改站点(入口地址不可改,需新建)")
async def patch_site(
    site_id: uuid.UUID,
    body: SitePatch,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await _get_site(session, site_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("enabled") and site.terms_status != TermsStatus.APPROVED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "来源使用条款未确认,不能启用")
    if "config" in data and body.config is not None:
        site.config = body.config.model_dump()
    for key in ("publisher", "interval_hours", "terms_url"):
        if key in data and data[key] is not None:
            setattr(site, key, data[key])
    if "enabled" in data and data["enabled"] is not None:
        site.enabled = data["enabled"]
        if site.enabled and site.next_run_at is None:
            site.next_run_at = datetime.now(UTC)
    await session.commit()
    return (await _attach_doc_summaries(session, [site]))[0]


@router.post("/{site_id}/terms", response_model=SiteOut, summary="确认/否决来源使用条款")
async def decide_terms(
    site_id: uuid.UUID,
    body: TermsDecision,
    p: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await _get_site(session, site_id)
    site.terms_status = body.status
    site.terms_note = body.note.strip()
    site.terms_decided_by = p.user_id
    site.terms_decided_at = datetime.now(UTC)
    if body.status != TermsStatus.APPROVED.value:
        site.enabled = False  # 条款被否决:立即停用
    await session.commit()
    return (await _attach_doc_summaries(session, [site]))[0]


@router.post("/{site_id}/run", status_code=202, summary="请求立即抓取(由 worker 执行)")
async def run_now(
    site_id: uuid.UUID,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    site = await _get_site(session, site_id)
    if site.terms_status != TermsStatus.APPROVED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "来源使用条款未确认,不能抓取")
    if not site.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "站点已停用")
    site.run_requested = True
    site.next_run_at = datetime.now(UTC)
    await session.commit()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/{site_id}/runs", response_model=list[RunOut], summary="运行记录")
async def list_runs(
    site_id: uuid.UUID,
    _: Principal = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session),
):
    await _get_site(session, site_id)
    stmt = (
        select(CrawlRun)
        .where(CrawlRun.site_id == site_id)
        .order_by(CrawlRun.started_at.desc())
        .limit(50)
    )
    return (await session.execute(stmt)).scalars().all()
