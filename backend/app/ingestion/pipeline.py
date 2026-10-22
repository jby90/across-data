"""采集流水线:发现 → 下载归档 → 解析 → 条款化。

每份文件独立事务、独立失败;原文先归档再解析,解析失败可重跑,不丢原文。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from posixpath import basename
from urllib.parse import unquote, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.archive import ArchiveStore
from app.ingestion.clauses import split_clauses
from app.ingestion.discovery import (
    DEFAULT_EGAZETTE_QUERIES,
    Candidate,
    extract_links,
    next_page_url,
    parse_egazette_hits,
    parse_egazette_search_config,
)
from app.ingestion.fetcher import BlockedError, FetchError, PoliteFetcher
from app.ingestion.models import (
    Clause,
    Document,
    DocumentVersion,
    PipelineEvent,
    VersionState,
)
from app.ingestion.ocr import LOW_CONFIDENCE, OcrEngine
from app.ingestion.parsing import (
    ParseError,
    parse_document,
    strip_margin_line_numbers,
    strip_running_headers,
    text_layer_garbled,
)
from app.sources.models import CrawlRun, RunStatus, SourceSite, Strategy, TermsStatus

log = logging.getLogger(__name__)

HARD_MAX_CANDIDATES = 500
HARD_MAX_INDEX_PAGES = 20
SessionFactory = async_sessionmaker[AsyncSession]


@dataclass
class Stats:
    pages_read: int = 0
    links_found: int = 0
    documents_new: int = 0
    versions_new: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SiteSnapshot:
    id: uuid.UUID
    country_code: str
    language: str
    document_type: str
    publisher: str
    entry_url: str
    strategy: str
    config: dict


def _now() -> datetime:
    return datetime.now(UTC)


def _title_from(candidate: Candidate) -> str:
    if candidate.title:
        return candidate.title[:500]
    name = basename(unquote(urlsplit(candidate.url).path))
    return (name or candidate.url)[:500]


async def discover(fetcher: PoliteFetcher, site: SiteSnapshot, stats: Stats) -> list[Candidate]:
    """发现候选原文。目录页本身读取失败会抛出异常,由调用方判定整次运行失败。"""
    if site.strategy == Strategy.DIRECT.value:
        return [Candidate(url=site.entry_url, title=site.publisher)]

    cfg = site.config
    cap = max(1, min(int(cfg.get("max_candidates", 200)), HARD_MAX_CANDIDATES))

    if site.strategy == Strategy.EGAZETTE.value:
        res = await fetcher.get(site.entry_url)
        stats.pages_read += 1
        assert res.content is not None
        app_id, index_name, api_key = parse_egazette_search_config(res.content)
        query_url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/{index_name}/query"
        queries = [str(q).strip() for q in (cfg.get("keywords") or []) if q and str(q).strip()]
        if not queries:
            queries = list(DEFAULT_EGAZETTE_QUERIES)
        found: dict[str, Candidate] = {}
        for q in queries:
            q_res = await fetcher.post_json(
                query_url,
                {
                    "query": q,
                    "advancedSyntax": True,
                    "restrictSearchableAttributes": ["title"],
                    "hitsPerPage": 100,
                },
                extra_headers={
                    "X-Algolia-Application-Id": app_id,
                    "X-Algolia-API-Key": api_key,
                },
            )
            stats.pages_read += 1
            if q_res.content:
                for cand in parse_egazette_hits(q_res.content):
                    found.setdefault(cand.url, cand)
        return list(found.values())[:cap]

    max_pages = max(1, min(int(cfg.get("max_pages", 1)), HARD_MAX_INDEX_PAGES))
    found = {}
    url: str | None = site.entry_url
    seen_pages: set[str] = set()
    for _ in range(max_pages):
        if url is None or url in seen_pages:
            break
        seen_pages.add(url)
        res = await fetcher.get(url)
        stats.pages_read += 1
        assert res.content is not None
        for cand in extract_links(
            res.content,
            res.url,
            selector=cfg.get("link_selector", "a[href]"),
            extensions=cfg.get("extensions"),
            keywords=cfg.get("keywords"),
        ):
            found.setdefault(cand.url, cand)
        url = next_page_url(res.content, res.url, cfg.get("next_selector"))
    return list(found.values())[:cap]


async def parse_version(
    session: AsyncSession,
    archive: ArchiveStore,
    version: DocumentVersion,
    language: str,
    ocr: OcrEngine | None = None,
) -> None:
    """解析某个版本并写入条款。可重复调用(会先清掉该版本旧条款)。不负责 commit。"""
    await session.execute(delete(Clause).where(Clause.version_id == version.id))
    try:
        content = archive.read(version.archive_path)
        parsed = await asyncio.to_thread(parse_document, content, version.mime_type)
        version.page_count = len(parsed.pages)
        version.char_count = parsed.char_count
        version.text_source = "text_layer"
        version.ocr_confidence = None
        source_pages = parsed.pages
        ocr_reason: str | None = None
        if parsed.looks_scanned:
            ocr_reason = "没有文字层,疑似扫描件"
        elif parsed.kind == "pdf":
            ocr_reason = text_layer_garbled(parsed.pages, language)
        if ocr_reason:
            if ocr is None or not ocr.available:
                version.state = VersionState.NEEDS_OCR.value
                version.parse_strategy = None
                version.error = f"{ocr_reason},需要 OCR"
                session.add(
                    PipelineEvent(
                        version_id=version.id, step="parse", status="ok", message="needs_ocr"
                    )
                )
                return
            result = await asyncio.to_thread(ocr.run, content, language)
            source_pages = result.pages
            version.text_source = "ocr"
            version.ocr_confidence = result.mean_confidence
            version.char_count = sum(len(p.strip()) for p in source_pages)
            note = "置信度偏低,务必人工核对" if result.mean_confidence < LOW_CONFIDENCE else ""
            session.add(
                PipelineEvent(
                    version_id=version.id,
                    step="ocr",
                    status="ok",
                    message=(
                        f"原因:{ocr_reason}。OCR {len(source_pages)} 页,语言 {result.languages},"
                        f"平均置信度 {result.mean_confidence}。OCR 文本需人工核对。{note}"
                    ),
                )
            )
        pages, removed = (
            strip_running_headers(source_pages) if parsed.kind == "pdf" else (source_pages, [])
        )
        if removed:
            session.add(
                PipelineEvent(
                    version_id=version.id,
                    step="clean",
                    status="ok",
                    message="移除重复页眉/页脚/页码: " + " | ".join(removed[:5]),
                )
            )
        if parsed.kind == "pdf":
            pages, margin_removed = strip_margin_line_numbers(pages)
            if margin_removed:
                session.add(
                    PipelineEvent(
                        version_id=version.id,
                        step="clean",
                        status="ok",
                        message=f"移除页边行号 {margin_removed} 个",
                    )
                )
        drafts, strategy = split_clauses(
            pages, paragraph_mode="line" if parsed.kind == "html" else "blank"
        )
        if not drafts:
            raise ParseError("没有解析出任何文字内容")
        for d in drafts:
            session.add(
                Clause(
                    version_id=version.id,
                    ordinal=d.ordinal,
                    clause_number=d.clause_number,
                    heading=d.heading,
                    section_path=d.section_path,
                    original_text=d.text,
                    page_start=d.page_start,
                    page_end=d.page_end,
                    text_hash=ArchiveStore.sha256(d.text.encode("utf-8")),
                    language=language,
                )
            )
        version.state = VersionState.PARSED.value
        version.parse_strategy = strategy
        version.error = None
        session.add(
            PipelineEvent(
                version_id=version.id,
                step="parse",
                status="ok",
                message=f"{len(drafts)} 条,策略 {strategy}",
            )
        )
    except Exception as e:  # noqa: BLE001 - 单个版本失败不能拖垮整次运行
        version.state = VersionState.FAILED.value
        version.error = f"{type(e).__name__}: {e}"[:2000]
        session.add(
            PipelineEvent(
                version_id=version.id, step="parse", status="error", message=version.error
            )
        )
        log.warning("解析失败 version=%s: %s", version.id, e)


async def _process_candidate(
    sf: SessionFactory,
    fetcher: PoliteFetcher,
    archive: ArchiveStore,
    site: SiteSnapshot,
    cand: Candidate,
    stats: Stats,
    ocr: OcrEngine | None = None,
) -> None:
    async with sf() as s:
        doc = (
            await s.execute(
                select(Document).where(Document.site_id == site.id, Document.source_url == cand.url)
            )
        ).scalar_one_or_none()
        if doc is None:
            doc = Document(
                site_id=site.id,
                country_code=site.country_code,
                source_url=cand.url,
                title=_title_from(cand),
                language=site.language,
                document_type=cand.document_type or site.document_type,
                instrument_family=cand.instrument_family,
                instrument_kind=cand.instrument_kind,
            )
            s.add(doc)
            await s.flush()
            stats.documents_new += 1
        else:
            if cand.instrument_family and not doc.instrument_family:
                doc.instrument_family = cand.instrument_family
            if cand.instrument_kind and not doc.instrument_kind:
                doc.instrument_kind = cand.instrument_kind
        latest = (
            await s.execute(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == doc.id)
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        try:
            res = await fetcher.get(
                cand.url,
                etag=latest.http_etag if latest else None,
                last_modified=latest.http_last_modified if latest else None,
            )
        except (BlockedError, FetchError) as e:
            doc.last_error = str(e)[:2000]
            doc.last_checked_at = _now()
            stats.errors += 1
            stats.messages.append(f"{cand.url}: {e}")
            await s.commit()
            return

        doc.last_checked_at = _now()
        doc.last_error = None
        if res.not_modified or res.content is None:
            await s.commit()
            return

        existing = (
            await s.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id == doc.id,
                    DocumentVersion.content_hash == ArchiveStore.sha256(res.content),
                )
            )
        ).first()
        if existing is not None:  # 内容与某个已有版本完全相同,不产生新版本
            await s.commit()
            return

        digest, rel = archive.put(res.content)
        version = DocumentVersion(
            document_id=doc.id,
            version_no=(latest.version_no + 1) if latest else 1,
            content_hash=digest,
            archive_path=rel,
            size_bytes=len(res.content),
            mime_type=res.mime or "application/octet-stream",
            final_url=res.url[:2048],
            http_etag=res.etag,
            http_last_modified=res.last_modified,
            published_date=cand.published_date,
            official_number=cand.official_number,
            metadata_source="official_index"
            if (cand.published_date or cand.official_number)
            else None,
        )
        s.add(version)
        await s.flush()
        s.add(
            PipelineEvent(
                version_id=version.id,
                step="download",
                status="ok",
                message=f"{len(res.content)} 字节,sha256={digest[:12]}",
            )
        )
        stats.versions_new += 1
        await parse_version(s, archive, version, site.language, ocr)
        await s.commit()


async def ingest_site(
    sf: SessionFactory,
    fetcher: PoliteFetcher,
    archive: ArchiveStore,
    site_id: uuid.UUID,
    *,
    trigger: str,
    ocr: OcrEngine | None = None,
) -> uuid.UUID:
    """执行一次站点采集,返回运行记录 id。"""
    async with sf() as s:
        site_row = await s.get(SourceSite, site_id)
        if site_row is None:
            raise LookupError(f"站点不存在: {site_id}")
        run = CrawlRun(site_id=site_id, trigger=trigger)
        s.add(run)
        await s.flush()
        run_id = run.id
        if site_row.terms_status != TermsStatus.APPROVED.value:
            run.status = RunStatus.SKIPPED.value
            run.finished_at = _now()
            run.message = "来源使用条款尚未确认,未执行抓取"
            await s.commit()
            return run_id
        site = SiteSnapshot(
            id=site_row.id,
            country_code=site_row.country_code,
            language=site_row.language,
            document_type=site_row.document_type,
            publisher=site_row.publisher,
            entry_url=site_row.entry_url,
            strategy=site_row.strategy,
            config=dict(site_row.config or {}),
        )
        await s.commit()

    stats = Stats()
    status = RunStatus.SUCCESS
    message: str | None = None
    try:
        candidates = await discover(fetcher, site, stats)
        stats.links_found = len(candidates)
        for cand in candidates:
            await _process_candidate(sf, fetcher, archive, site, cand, stats, ocr)
        if stats.errors:
            status = RunStatus.PARTIAL
            message = "; ".join(stats.messages[:5])
        elif not candidates:
            status = RunStatus.PARTIAL
            message = "未发现任何原文链接,请检查站点配置或页面结构是否变化"
    except (BlockedError, FetchError) as e:
        status = RunStatus.FAILED
        message = str(e)[:2000]
    except Exception as e:  # noqa: BLE001
        status = RunStatus.FAILED
        message = f"{type(e).__name__}: {e}"[:2000]
        log.exception("站点采集异常 site=%s", site_id)

    async with sf() as s:
        run = await s.get(CrawlRun, run_id)
        site_row = await s.get(SourceSite, site_id)
        assert run is not None and site_row is not None
        run.status = status.value
        run.finished_at = _now()
        run.pages_read = stats.pages_read
        run.links_found = stats.links_found
        run.documents_new = stats.documents_new
        run.versions_new = stats.versions_new
        run.errors = stats.errors
        run.message = message
        site_row.last_run_at = run.finished_at
        site_row.last_status = status.value
        site_row.consecutive_failures = (
            0
            if status in (RunStatus.SUCCESS, RunStatus.PARTIAL)
            else site_row.consecutive_failures + 1
        )
        await s.commit()
    return run_id
