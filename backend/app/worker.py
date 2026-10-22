"""后台 worker:定时轮询到期站点并执行采集。

用法:
  python -m app.worker           # 常驻
  python -m app.worker --once    # 跑一轮就退出(便于验证)
"""

import argparse
import asyncio
import logging

import httpx

from app import models_registry  # noqa: F401  确保所有模型已注册
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.ingestion.fetcher import PoliteFetcher
from app.ingestion.ocr import OcrEngine
from app.sources.scheduler import run_due_once

log = logging.getLogger("app.worker")


async def main(once: bool) -> None:
    settings = get_settings()
    archive = ArchiveStore(settings.archive_dir)
    ocr = OcrEngine(settings.tesseract_cmd, settings.tessdata_dir, settings.ocr_dpi)
    log.info("OCR %s", "可用" if ocr.available else "不可用(扫描件只标记,不识别)")
    sf = get_sessionmaker()
    async with httpx.AsyncClient(timeout=settings.crawl_timeout_seconds) as client:

        def make_fetcher() -> PoliteFetcher:
            # 每次运行新建,robots.txt 缓存不会跨运行过期
            return PoliteFetcher(
                client,
                user_agent=settings.crawl_user_agent,
                delay_seconds=settings.crawl_delay_seconds,
                max_bytes=settings.crawl_max_bytes,
                allow_resolved_cidrs=settings.crawl_allow_resolved_cidrs,
            )

        while True:
            runs = await run_due_once(sf, make_fetcher, archive, ocr=ocr)
            if runs:
                log.info("本轮执行了 %d 个站点", runs)
            if once:
                return
            await asyncio.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(prog="app.worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.once))
    except KeyboardInterrupt:
        pass
