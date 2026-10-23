import hashlib
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion, PipelineEvent
from app.ingestion.pipeline import ingest_site, parse_version
from app.sources.models import CrawlRun, SourceSite, TermsStatus

from .helpers import make_fetcher, make_pdf

INDEX = """<html><body>
<a href="/laws/act.pdf">Data Act</a>
<a href="/laws/guide.html">Guide page</a>
<a href="/laws/archive.zip">zip</a>
</body></html>"""

HTML_GUIDE = (
    b"<html><body><p>1.1 The Act governs data.</p><p>1.2 It applies widely.</p></body></html>"
)


def act_pdf(extra: str = "") -> bytes:
    return make_pdf(
        [
            ["Article 1 Scope", "This Act applies to vehicle data." + extra],
            ["Article 2 Transfer", "Cross-border transfer needs approval."],
        ]
    )


class Site:
    """可变的模拟站点,让测试能中途改内容。"""

    def __init__(self):
        self.files: dict[str, httpx.Response | None] = {}
        self.robots = ""
        self.index = INDEX
        self.hits: list[str] = []
        self.pdf = act_pdf()
        self.etag = '"v1"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path == "/index":
            if self.index is None:
                return httpx.Response(500)
            return httpx.Response(200, text=self.index, headers={"content-type": "text/html"})
        if path == "/laws/act.pdf":
            if request.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(
                200,
                content=self.pdf,
                headers={"content-type": "application/pdf", "etag": self.etag},
            )
        if path == "/laws/guide.html":
            return httpx.Response(200, content=HTML_GUIDE, headers={"content-type": "text/html"})
        return httpx.Response(404)


async def add_site(
    sf,
    *,
    terms=TermsStatus.APPROVED,
    strategy="INDEX_PAGE",
    config=None,
    entry="https://example.org/index",
):
    async with sf() as s:
        site = SourceSite(
            country_code="SG",
            publisher="测试机构",
            language="en",
            document_type="LAW",
            entry_url=entry,
            strategy=strategy,
            config=config if config is not None else {"extensions": [".pdf", ".html"]},
            terms_status=terms.value,
            enabled=terms == TermsStatus.APPROVED,
        )
        s.add(site)
        await s.commit()
        return site.id


async def run(sf, tmp_path, site_state: Site, site_id, trigger="manual"):
    archive = ArchiveStore(tmp_path / "archive")
    run_id = await ingest_site(
        sf, make_fetcher(site_state.handler), archive, site_id, trigger=trigger
    )
    async with sf() as s:
        return await s.get(CrawlRun, run_id), archive


async def test_first_run_discovers_downloads_parses(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(session_factory)
    r, archive = await run(session_factory, tmp_path, state, site_id)

    assert (
        r.status == "success"
        and r.links_found == 2
        and r.documents_new == 2
        and r.versions_new == 2
    )
    async with session_factory() as s:
        docs = {d.source_url: d for d in (await s.execute(select(Document))).scalars()}
        assert set(docs) == {
            "https://example.org/laws/act.pdf",
            "https://example.org/laws/guide.html",
        }
        pdf_ver = (
            await s.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == docs["https://example.org/laws/act.pdf"].id
                )
            )
        ).scalar_one()
        assert (
            pdf_ver.state == "parsed"
            and pdf_ver.parse_strategy == "numbered"
            and pdf_ver.page_count == 2
        )
        assert pdf_ver.content_hash == hashlib.sha256(state.pdf).hexdigest()
        clauses = (
            (
                await s.execute(
                    select(Clause).where(Clause.version_id == pdf_ver.id).order_by(Clause.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert [c.clause_number for c in clauses] == ["1", "2"]
        assert clauses[0].page_start == 1 and clauses[1].page_start == 2
        assert "vehicle data" in clauses[0].original_text
        assert clauses[0].text_hash == hashlib.sha256(clauses[0].original_text.encode()).hexdigest()
        # 原文已归档且字节一致
        assert archive.read(pdf_ver.archive_path) == state.pdf
        events = (
            await s.execute(
                select(PipelineEvent.step, PipelineEvent.status).where(
                    PipelineEvent.version_id == pdf_ver.id
                )
            )
        ).all()
        assert ("download", "ok") in events and ("parse", "ok") in events

        html_ver = (
            await s.execute(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == docs["https://example.org/laws/guide.html"].id
                )
            )
        ).scalar_one()
        assert html_ver.state == "parsed" and html_ver.parse_strategy == "numbered"
        site = await s.get(SourceSite, site_id)
        assert site.last_status == "success" and site.consecutive_failures == 0


async def test_download_success_parse_failure_is_partial_and_preserves_raw(
    session_factory, tmp_path
):
    state = Site()
    state.pdf = b"%PDF-1.7 not a complete valid PDF"
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf"
    )
    result, archive = await run(session_factory, tmp_path, state, site_id)
    assert result.status == "partial" and result.errors == 1
    assert result.versions_new == 1 and "Parse" in result.message
    async with session_factory() as session:
        version = (await session.scalars(select(DocumentVersion))).one()
        doc = await session.get(Document, version.document_id)
        assert version.state == "failed" and doc.last_error == version.error
        assert archive.read(version.archive_path) == state.pdf
        assert await session.scalar(select(func.count()).select_from(Clause)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PipelineEvent)
                .where(PipelineEvent.status == "error")
            )
            == 1
        )


async def test_rerun_unchanged_creates_nothing_new(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(session_factory)
    await run(session_factory, tmp_path, state, site_id)
    r2, _ = await run(session_factory, tmp_path, state, site_id)
    assert r2.status == "success" and r2.documents_new == 0 and r2.versions_new == 0
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(DocumentVersion))).scalar() == 2


async def test_changed_content_creates_new_version_and_keeps_old(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(session_factory)
    await run(session_factory, tmp_path, state, site_id)

    state.pdf = act_pdf(" Amended in 2026.")
    state.etag = '"v2"'  # 服务端内容已变,旧 etag 不再命中 304
    r2, archive = await run(session_factory, tmp_path, state, site_id)
    assert r2.versions_new == 1 and r2.documents_new == 0

    async with session_factory() as s:
        doc = (
            await s.execute(select(Document).where(Document.source_url.like("%act.pdf")))
        ).scalar_one()
        versions = (
            (
                await s.execute(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == doc.id)
                    .order_by(DocumentVersion.version_no)
                )
            )
            .scalars()
            .all()
        )
        assert [v.version_no for v in versions] == [1, 2]
        assert versions[0].content_hash != versions[1].content_hash
        # 旧版本的条款与原文都还在,新版本有自己的条款
        old_clause = (
            await s.execute(
                select(Clause).where(Clause.version_id == versions[0].id, Clause.ordinal == 0)
            )
        ).scalar_one()
        new_clause = (
            await s.execute(
                select(Clause).where(Clause.version_id == versions[1].id, Clause.ordinal == 0)
            )
        ).scalar_one()
        assert "Amended" not in old_clause.original_text and "Amended" in new_clause.original_text
        assert archive.read(versions[0].archive_path) != archive.read(versions[1].archive_path)


async def test_conditional_get_skips_download_when_not_modified(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    await run(session_factory, tmp_path, state, site_id)
    r2, _ = await run(session_factory, tmp_path, state, site_id)  # etag 命中 -> 304
    assert r2.versions_new == 0 and r2.status == "success"
    async with session_factory() as s:
        doc = (await s.execute(select(Document))).scalar_one()
        assert doc.last_checked_at is not None and doc.last_error is None


async def test_reverted_content_does_not_violate_unique_hash(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    original = state.pdf
    await run(session_factory, tmp_path, state, site_id)
    state.pdf, state.etag = act_pdf(" changed"), '"v2"'
    await run(session_factory, tmp_path, state, site_id)
    state.pdf, state.etag = original, '"v3"'  # 内容改回第一版
    r3, _ = await run(session_factory, tmp_path, state, site_id)
    assert r3.status == "success" and r3.versions_new == 0


async def test_scanned_pdf_marked_needs_ocr_without_clauses(session_factory, tmp_path):
    state = Site()
    state.pdf = make_pdf([[], []])
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    await run(session_factory, tmp_path, state, site_id)
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        assert v.state == "needs_ocr" and "OCR" in v.error
        assert (await s.execute(select(func.count()).select_from(Clause))).scalar() == 0


async def test_one_failing_document_does_not_stop_the_others(session_factory, tmp_path):
    state = Site()
    state.index = INDEX.replace("/laws/archive.zip", "/laws/gone.pdf")
    site_id = await add_site(session_factory)
    r, _ = await run(session_factory, tmp_path, state, site_id)
    assert r.status == "partial" and r.errors == 1 and r.versions_new == 2
    assert "gone.pdf" in r.message
    async with session_factory() as s:
        gone = (
            await s.execute(select(Document).where(Document.source_url.like("%gone.pdf")))
        ).scalar_one()
        assert "404" in gone.last_error
        site = await s.get(SourceSite, site_id)
        assert site.consecutive_failures == 0  # partial 不算整站失败


async def test_robots_disallowed_document_recorded_not_fetched(session_factory, tmp_path):
    state = Site()
    state.robots = "User-agent: *\nDisallow: /laws/guide.html"
    site_id = await add_site(session_factory)
    r, _ = await run(session_factory, tmp_path, state, site_id)
    assert r.status == "partial" and r.versions_new == 1
    assert "/laws/guide.html" not in state.hits
    async with session_factory() as s:
        d = (
            await s.execute(select(Document).where(Document.source_url.like("%guide.html")))
        ).scalar_one()
        assert "robots" in d.last_error


async def test_terms_not_approved_means_no_network_at_all(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(session_factory, terms=TermsStatus.PENDING)
    r, _ = await run(session_factory, tmp_path, state, site_id)
    assert r.status == "skipped" and "条款" in r.message
    assert state.hits == []
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Document))).scalar() == 0


async def test_index_page_failure_fails_run_and_counts(session_factory, tmp_path):
    state = Site()
    state.index = None  # 目录页 500
    site_id = await add_site(session_factory)
    r, _ = await run(session_factory, tmp_path, state, site_id)
    assert r.status == "failed"
    assert r.errors >= 1
    async with session_factory() as s:
        site = await s.get(SourceSite, site_id)
        assert site.last_status == "failed" and site.consecutive_failures == 1
    r2, _ = await run(session_factory, tmp_path, state, site_id)
    async with session_factory() as s:
        assert (await s.get(SourceSite, site_id)).consecutive_failures == 2


async def test_empty_discovery_is_flagged_not_green(session_factory, tmp_path):
    state = Site()
    state.index = "<html><body>no links</body></html>"
    site_id = await add_site(session_factory)
    r, _ = await run(session_factory, tmp_path, state, site_id)
    assert r.status == "partial" and "未发现" in r.message


async def test_keywords_and_pagination(session_factory, tmp_path):
    pages = {
        "/index": '<a href="/laws/act.pdf">Data Act</a><a href="/p2" class="next">n</a>',
        "/p2": '<a href="/laws/other.pdf">Other thing</a>',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        return httpx.Response(200, content=act_pdf(), headers={"content-type": "application/pdf"})

    archive = ArchiveStore(tmp_path / "a")
    site_id = await add_site(
        session_factory, config={"max_pages": 2, "next_selector": "a.next", "keywords": ["act"]}
    )
    run_id = await ingest_site(
        session_factory, make_fetcher(handler), archive, site_id, trigger="manual"
    )
    async with session_factory() as s:
        r = await s.get(CrawlRun, run_id)
        assert r.pages_read == 2 and r.links_found == 1  # 关键词只保留 Data Act


async def test_reparse_preserves_clauses_and_detects_tampered_archive(session_factory, tmp_path):
    state = Site()
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    _, archive = await run(session_factory, tmp_path, state, site_id)
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        await parse_version(s, archive, v, "en")  # 重跑不重复写条款
        await s.commit()
        assert (
            await s.execute(
                select(func.count()).select_from(Clause).where(Clause.version_id == v.id)
            )
        ).scalar() == 2

        # 篡改归档文件:重解析必须失败并留痕,而不是悄悄用被改过的内容
        path = tmp_path / "archive" / v.archive_path
        path.write_bytes(b"tampered")
        await parse_version(s, archive, v, "en")
        await s.commit()
        assert v.state == "failed" and "哈希" in v.error
        assert (
            await s.execute(
                select(func.count()).select_from(Clause).where(Clause.version_id == v.id)
            )
        ).scalar() == 2  # 完整性失败隔离该版本，但保留历史条款和审批引用


async def test_unsupported_file_type_fails_parse_but_keeps_original(session_factory, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, content=b"PK\x03\x04zipdata", headers={"content-type": "application/zip"}
        )

    archive = ArchiveStore(tmp_path / "a")
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/x.zip", config={}
    )
    await ingest_site(session_factory, make_fetcher(handler), archive, site_id, trigger="manual")
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        assert v.state == "failed" and archive.read(v.archive_path) == b"PK\x03\x04zipdata"


async def test_db_forbids_enabled_site_without_approved_terms(session_factory):
    import pytest
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as s:
        s.add(
            SourceSite(
                country_code="SG",
                publisher="x",
                language="en",
                document_type="LAW",
                entry_url="https://example.org/",
                strategy="DIRECT",
                enabled=True,
                terms_status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_unknown_site_raises(session_factory, tmp_path):
    import pytest

    with pytest.raises(LookupError):
        await ingest_site(
            session_factory,
            make_fetcher(Site().handler),
            ArchiveStore(tmp_path),
            uuid.uuid4(),
            trigger="manual",
        )


def test_archive_put_is_idempotent_and_confined(tmp_path):
    import pytest

    from app.ingestion.archive import ArchiveIntegrityError

    a = ArchiveStore(tmp_path)
    h1, p1 = a.put(b"abc")
    h2, p2 = a.put(b"abc")
    assert (h1, p1) == (h2, p2) and a.read(p1) == b"abc"
    with pytest.raises(ArchiveIntegrityError):
        a.read("../../etc/passwd")
    _ = datetime.now(UTC)


async def test_egazette_strategy_discovers_via_public_search_config_and_ingests(
    session_factory, tmp_path
):
    """新加坡电子宪报 EGAZETTE 策略:先读目录页提取公开检索配置,再按关键词检索并下载增刊 PDF,同时过滤掉法案草案(Bills)。"""
    import json
    from datetime import date

    browse_html = (
        '<html><script>self.__next_f.push([1,"{\\"config\\":{\\"type\\":\\"egazette-algolia\\",'
        '\\"appId\\":\\"1V7DZGZJKK\\",\\"indexName\\":\\"prod_ogp_egazettes_index\\",'
        '\\"searchApiKey\\":\\"pub-key-123\\"}}"])</script></html>'
    )
    algolia_hits = {
        "hits": [
            {
                "title": "Road Traffic Act 1961 - Road Traffic (Autonomous Motor Vehicles) (Amendment) Rules 2024",
                "subCategory": "Subsidiary Legislation Supplement",
                "notificationNum": "089",
                "publishDate": "14/02/2024",
                "fileUrl": "https://assets.egazette.gov.sg/2024/089.pdf",
            },
            {
                "title": "Personal Data Protection Bill",
                "subCategory": "Bills Supplement",  # 法案草案,不应被当成生效法律收录
                "fileUrl": "https://assets.egazette.gov.sg/2024/bill.pdf",
            },
            {
                "title": "Evil external file",
                "subCategory": "Acts Supplement",
                "fileUrl": "https://evil.example.org/2024/bad.pdf",  # 非官方宪报资源域名,拒绝
            },
        ],
        "nbHits": 3,
    }
    pdf_bytes = act_pdf(" Autonomous vehicles rules.")
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if str(request.url) == "https://www.egazette.gov.sg/egazette-browse/":
            return httpx.Response(200, text=browse_html, headers={"content-type": "text/html"})
        if request.url.host == "1v7dzgzjkk-dsn.algolia.net":
            seen_headers["app"] = request.headers.get("x-algolia-application-id", "")
            seen_headers["key"] = request.headers.get("x-algolia-api-key", "")
            return httpx.Response(
                200,
                content=json.dumps(algolia_hits).encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        if str(request.url) == "https://assets.egazette.gov.sg/2024/089.pdf":
            return httpx.Response(
                200, content=pdf_bytes, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)

    site_id = await add_site(
        session_factory,
        strategy="EGAZETTE",
        entry="https://www.egazette.gov.sg/egazette-browse/",
        config={"keywords": ['"Road Traffic (Autonomous Motor Vehicles)"']},
    )
    archive = ArchiveStore(tmp_path / "archive")
    run_id = await ingest_site(
        session_factory, make_fetcher(handler), archive, site_id, trigger="manual"
    )
    assert seen_headers == {"app": "1V7DZGZJKK", "key": "pub-key-123"}

    async with session_factory() as s:
        r = await s.get(CrawlRun, run_id)
        assert r.status == "success" and r.pages_read == 2 and r.links_found == 1
        assert r.documents_new == 1 and r.versions_new == 1
        doc = (await s.execute(select(Document))).scalar_one()
        assert doc.source_url == "https://assets.egazette.gov.sg/2024/089.pdf"
        assert doc.document_type == "REGULATION"
        assert doc.instrument_family == "sg-av-rules"
        assert doc.instrument_kind == "AMENDMENT"
        ver = (await s.execute(select(DocumentVersion))).scalar_one()
        assert ver.published_date == date(2024, 2, 14)
        assert ver.official_number == "089"
        assert ver.metadata_source == "official_index"
        assert ver.state == "parsed"
