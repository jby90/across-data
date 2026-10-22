import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.ingestion.archive import ArchiveStore
from app.sources.models import CrawlRun, SourceSite
from app.sources.scheduler import LEASE, claim_due_site, compute_next_run, run_due_once

from .helpers import make_fetcher
from .test_pipeline import Site

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


async def add(
    sf, *, url, due: datetime | None, enabled=True, terms="approved", interval=24, requested=False
):
    async with sf() as s:
        site = SourceSite(
            country_code="SG",
            publisher="p",
            language="en",
            document_type="LAW",
            entry_url=url,
            strategy="INDEX_PAGE",
            config={"extensions": [".pdf", ".html"]},
            enabled=enabled,
            terms_status=terms,
            next_run_at=due,
            interval_hours=interval,
            run_requested=requested,
        )
        s.add(site)
        await s.commit()
        return site.id


async def test_claims_only_due_enabled_approved_sites(session_factory):
    due = await add(
        session_factory, url="https://a.example.org/index", due=NOW - timedelta(minutes=1)
    )
    await add(
        session_factory, url="https://b.example.org/index", due=NOW + timedelta(hours=1)
    )  # 未到期
    await add(
        session_factory,
        url="https://c.example.org/index",
        due=NOW - timedelta(hours=1),
        enabled=False,
        terms="pending",
    )
    await add(session_factory, url="https://d.example.org/index", due=None)  # 从未排期
    claimed = await claim_due_site(session_factory, NOW)
    assert claimed is not None and claimed[0] == due and claimed[1] == "schedule"
    assert await claim_due_site(session_factory, NOW) is None


async def test_claim_sets_lease_and_clears_manual_flag(session_factory):
    sid = await add(session_factory, url="https://a.example.org/index", due=NOW, requested=True)
    claimed = await claim_due_site(session_factory, NOW)
    assert claimed == (sid, "manual")
    async with session_factory() as s:
        site = await s.get(SourceSite, sid)
        assert site.next_run_at == NOW + LEASE and site.run_requested is False
    # 租约期内不会被再次领取;租约到期后(worker 崩溃的情形)可以重新领取
    assert await claim_due_site(session_factory, NOW + LEASE - timedelta(seconds=1)) is None
    assert (await claim_due_site(session_factory, NOW + LEASE))[0] == sid


async def test_concurrent_workers_never_claim_the_same_site(session_factory):
    ids = {
        await add(session_factory, url=f"https://s{i}.example.org/index", due=NOW) for i in range(3)
    }
    results = await asyncio.gather(*[claim_due_site(session_factory, NOW) for _ in range(6)])
    got = [r[0] for r in results if r is not None]
    assert len(got) == 3 and set(got) == ids  # 3 个站点各被领取一次,无重复


async def test_concurrent_claim_of_single_site_has_one_winner(session_factory):
    await add(session_factory, url="https://only.example.org/index", due=NOW)
    results = await asyncio.gather(*[claim_due_site(session_factory, NOW) for _ in range(5)])
    assert sum(r is not None for r in results) == 1


def test_compute_next_run_interval_and_backoff():
    assert compute_next_run(
        NOW, interval_hours=24, status="success", consecutive_failures=0
    ) == NOW + timedelta(hours=24)
    assert compute_next_run(
        NOW, interval_hours=24, status="partial", consecutive_failures=0
    ) == NOW + timedelta(hours=24)
    # 失败:15 分钟起指数退避,不超过正常间隔
    assert compute_next_run(
        NOW, interval_hours=24, status="failed", consecutive_failures=1
    ) == NOW + timedelta(minutes=15)
    assert compute_next_run(
        NOW, interval_hours=24, status="failed", consecutive_failures=3
    ) == NOW + timedelta(minutes=60)
    assert compute_next_run(
        NOW, interval_hours=1, status="failed", consecutive_failures=10
    ) == NOW + timedelta(hours=1)


async def test_run_due_once_runs_and_reschedules(session_factory, tmp_path):
    state = Site()
    sid = await add(
        session_factory,
        url="https://example.org/index",
        due=NOW - timedelta(minutes=5),
        interval=48,
    )
    archive = ArchiveStore(tmp_path / "a")
    runs = await run_due_once(
        session_factory, lambda: make_fetcher(state.handler), archive, now_fn=lambda: NOW
    )
    assert runs == 1
    async with session_factory() as s:
        site = await s.get(SourceSite, sid)
        assert site.next_run_at == NOW + timedelta(hours=48) and site.last_status == "success"
        run = (await s.execute(select(CrawlRun).where(CrawlRun.site_id == sid))).scalar_one()
        assert run.trigger == "schedule" and run.versions_new == 2
    # 再跑一轮:没有到期站点
    assert (
        await run_due_once(
            session_factory, lambda: make_fetcher(state.handler), archive, now_fn=lambda: NOW
        )
        == 0
    )


async def test_failed_site_backs_off_and_loop_survives_exceptions(session_factory, tmp_path):
    bad = await add(
        session_factory, url="https://bad.example.org/index", due=NOW - timedelta(minutes=2)
    )
    good = await add(
        session_factory, url="https://example.org/index", due=NOW - timedelta(minutes=1)
    )
    state = Site()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.example.org":
            raise RuntimeError("unexpected failure inside transport")
        return state.handler(request)

    archive = ArchiveStore(tmp_path / "a")
    runs = await run_due_once(
        session_factory, lambda: make_fetcher(handler), archive, now_fn=lambda: NOW
    )
    assert runs == 2  # 一个站点出错不会让循环中断
    async with session_factory() as s:
        b = await s.get(SourceSite, bad)
        g = await s.get(SourceSite, good)
        assert b.last_status == "failed" and b.consecutive_failures == 1
        assert b.next_run_at == NOW + timedelta(minutes=15)
        assert g.last_status == "success"
