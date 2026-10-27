"""站点调度:基于 PostgreSQL 的到期领取(FOR UPDATE SKIP LOCKED),多 worker 安全,无需额外中间件。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.ingestion.archive import ArchiveStore
from app.ingestion.fetcher import PoliteFetcher
from app.ingestion.ocr import OcrEngine
from app.ingestion.pipeline import ingest_site
from app.sources.models import RunStatus, SourceSite, TermsStatus

log = logging.getLogger(__name__)

# 领取后先把 next_run_at 推后一个租约期:worker 中途崩溃,租约到期后会被重新领取
LEASE = timedelta(minutes=30)
BACKOFF_BASE = timedelta(minutes=15)

SessionFactory = async_sessionmaker[AsyncSession]


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_due_site(
    sf: SessionFactory, now: datetime | None = None
) -> tuple[uuid.UUID, str] | None:
    """领取一个到期站点。返回 (站点 id, 触发方式) 或 None。"""
    now = now or _now()
    async with sf() as s:
        site = (
            await s.execute(
                select(SourceSite)
                .where(
                    SourceSite.enabled.is_(True),
                    SourceSite.terms_status == TermsStatus.APPROVED.value,
                    (SourceSite.terms_approval_scope == "production")
                    if get_settings().env != "dev"
                    else True,
                    SourceSite.next_run_at.is_not(None),
                    SourceSite.next_run_at <= now,
                )
                .order_by(SourceSite.next_run_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if site is None:
            return None
        trigger = "manual" if site.run_requested else "schedule"
        site.run_requested = False
        site.next_run_at = now + LEASE
        await s.commit()
        return site.id, trigger


def compute_next_run(
    now: datetime, *, interval_hours: int, status: str | None, consecutive_failures: int
) -> datetime:
    interval = timedelta(hours=interval_hours)
    if status == RunStatus.FAILED.value and consecutive_failures > 0:
        backoff = BACKOFF_BASE * (2 ** (consecutive_failures - 1))
        return now + min(interval, backoff)
    return now + interval


async def run_due_once(
    sf: SessionFactory,
    make_fetcher: Callable[[], PoliteFetcher],
    archive: ArchiveStore,
    *,
    now_fn: Callable[[], datetime] = _now,
    ocr: OcrEngine | None = None,
) -> int:
    """把当前所有到期站点各跑一次,返回运行次数。"""
    runs = 0
    while True:
        claimed = await claim_due_site(sf, now_fn())
        if claimed is None:
            return runs
        site_id, trigger = claimed
        try:
            await ingest_site(sf, make_fetcher(), archive, site_id, trigger=trigger, ocr=ocr)
        except Exception:  # noqa: BLE001 - 单个站点异常不能让调度循环退出
            log.exception("站点运行异常 site=%s", site_id)
        runs += 1
        async with sf() as s:
            site = await s.get(SourceSite, site_id)
            if site is not None:
                site.next_run_at = compute_next_run(
                    now_fn(),
                    interval_hours=site.interval_hours,
                    status=site.last_status,
                    consecutive_failures=site.consecutive_failures,
                )
                await s.commit()
