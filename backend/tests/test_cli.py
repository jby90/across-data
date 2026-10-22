import json
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app.cli import load_sites, render_report
from app.sources.models import SourceSite
from app.sources.schemas import SiteCreate

from .helpers import make_fetcher

SEED = Path(__file__).resolve().parents[2] / "data" / "seed" / "source_sites.json"


def test_seed_file_is_valid_unique_and_covers_five_countries():
    """种子文件本身必须通过站点校验、不重复,并覆盖首批五国。"""
    items = json.loads(SEED.read_text(encoding="utf-8"))
    keys = set()
    for raw in items:
        body = SiteCreate(
            country_code=raw["country_code"],
            publisher=raw["publisher"],
            language=raw["language"],
            document_type=raw["document_type"],
            entry_url=raw["entry_url"],
            strategy=raw.get("strategy", "DIRECT"),
        )
        keys.add((body.country_code, body.entry_url))
        assert raw.get("note"), f"每条种子要写明来源依据: {raw['entry_url']}"
    assert len(keys) == len(items)
    assert {c for c, _ in keys} == {"SG", "EG", "TN", "MA", "LB"}


async def test_load_sites_is_idempotent_and_never_enables(session_factory):
    created, skipped = await load_sites(SEED, session_factory)
    assert created > 0 and skipped == 0
    created2, skipped2 = await load_sites(SEED, session_factory)
    assert created2 == 0 and skipped2 == created
    async with session_factory() as s:
        sites = (await s.execute(select(SourceSite))).scalars().all()
        assert len(sites) == created
        assert all(not x.enabled and x.terms_status == "pending" for x in sites)
        assert all(x.next_run_at is None for x in sites)  # 不会被调度器领取
        assert (await s.execute(select(func.count()).select_from(SourceSite))).scalar() == created


async def test_loaded_seed_sites_are_never_claimed_by_scheduler(session_factory):
    from app.sources.scheduler import claim_due_site

    await load_sites(SEED, session_factory)
    assert await claim_due_site(session_factory) is None


async def test_preflight_reports_without_downloading_files():
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        return httpx.Response(200, content=b"SHOULD NOT BE DOWNLOADED")

    f = make_fetcher(handler)
    ok = await f.preflight("https://example.org/laws/a.pdf")
    no = await f.preflight("https://example.org/private/b.pdf")
    internal = await f.preflight("http://127.0.0.1/x.pdf")
    assert ok["robots"] == "允许" and no["robots"] == "不允许/无法确认"
    assert internal["target"].startswith("拦截")
    assert hits == ["/robots.txt"]  # 只访问了 robots.txt,没有下载任何文件


async def test_preflight_robots_5xx_is_reported_as_unconfirmed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    res = await make_fetcher(handler).preflight("https://example.org/a.pdf")
    assert res["robots"] == "不允许/无法确认" and "500" in res["robots_detail"]


def test_render_report_marks_no_download_and_caveat():
    md = render_report(
        [
            {
                "country": "EG",
                "publisher": "x",
                "url": "https://e.org/a.pdf",
                "target": "ok",
                "robots": "允许",
                "robots_detail": "HTTP 200,已读取规则",
                "terms": "pending",
            }
        ]
    )
    assert "没有下载任何法规文件" in md and "不等于" in md and "https://e.org/a.pdf" in md
