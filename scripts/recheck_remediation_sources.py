"""Recheck already approved four-country sites in an isolated remediation copy only.

Does not approve terms, bypass robots, rewrite clauses, or replace source versions.
Requires CYSH_DATABASE_URL / CYSH_ARCHIVE_DIR in the environment. Run from backend.
"""

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx
from sqlalchemy import select

from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.ingestion.fetcher import PoliteFetcher
from app.ingestion.pipeline import ingest_site
from app.sources.models import CrawlRun, SourceSite
from app.sources.permissions import terms_permit_crawl


async def main():
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only an isolated remediation database is allowed")
    archive_root = Path(settings.archive_dir).resolve()
    if "backups" in archive_root.parts:
        raise ValueError("backup archives are read-only; copy to a working archive first")
    sf = get_sessionmaker()
    async with sf() as session:
        sites = (await session.scalars(select(SourceSite).where(
            SourceSite.country_code.in_(["EG", "TN", "MA", "LB"])
        ).order_by(SourceSite.country_code, SourceSite.id))).all()
        ids = [site.id for site in sites if site.enabled and terms_permit_crawl(site)]
    proxies = urllib.request.getproxies()
    proxy = os.environ.get("HTTPS_PROXY") or proxies.get("https")
    async with httpx.AsyncClient(timeout=30, proxy=proxy) as client:
        for site_id in ids:
            fetcher = PoliteFetcher(client, user_agent=settings.crawl_user_agent,
                delay_seconds=settings.crawl_delay_seconds, max_bytes=settings.crawl_max_bytes,
                allow_resolved_cidrs=settings.crawl_allow_resolved_cidrs)
            run_id = await ingest_site(sf, fetcher, ArchiveStore(archive_root), site_id,
                                       trigger="manual")
            async with sf() as session:
                run = await session.get(CrawlRun, run_id)
                print(json.dumps(dict(site_id=str(site_id), run_id=str(run_id),
                    status=run.status, errors=run.errors, new_versions=run.versions_new,
                    message=run.message), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
