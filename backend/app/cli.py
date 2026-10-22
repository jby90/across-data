"""命令行工具。

用法:
  python -m app.cli create-platform-admin --email admin@example.com --name 管理员
  python -m app.cli load-sites ../data/seed/source_sites.json   # 导入种子站点(停用、条款待确认)
  python -m app.cli preflight [--country EG] [--report 报告.md]  # 只读预检,不下载文件

密码从环境变量 CYSH_ADMIN_PASSWORD 读取,未设置则交互输入(不回显)。
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app import models_registry  # noqa: F401  确保所有模型已注册
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.iam import service
from app.iam.models import Role


async def _create_platform_admin(email: str, name: str, password: str) -> None:
    async with get_sessionmaker()() as session:
        user = await service.create_user(
            session,
            email=email,
            display_name=name,
            password=password,
            role=Role.PLATFORM_ADMIN,
            org_id=None,
        )
        print(f"已创建平台管理员: {user.email}")


async def load_sites(path: Path, sf=None) -> tuple[int, int]:
    """导入种子站点。已存在的(同国家同入口地址)跳过。返回 (新增数, 跳过数)。

    种子只承载"去哪里取",不承载"可以取":导入后一律是停用、条款待确认,
    必须由人在管理端确认条款后才能启用。
    """
    from sqlalchemy import select

    from app.sources.models import SourceSite
    from app.sources.schemas import SiteCreate

    items = json.loads(path.read_text(encoding="utf-8"))
    created = skipped = 0
    async with (sf or get_sessionmaker())() as session:
        for raw in items:
            body = SiteCreate(
                country_code=raw["country_code"],
                publisher=raw["publisher"],
                language=raw["language"],
                document_type=raw["document_type"],
                entry_url=raw["entry_url"],
                strategy=raw.get("strategy", "DIRECT"),
                config=raw.get("config", {}),
                interval_hours=raw.get("interval_hours", 168),
                terms_url=raw.get("terms_url"),
            )
            exists = (
                await session.execute(
                    select(SourceSite.id).where(
                        SourceSite.country_code == body.country_code,
                        SourceSite.entry_url == body.entry_url,
                    )
                )
            ).first()
            if exists:
                skipped += 1
                continue
            session.add(
                SourceSite(
                    country_code=body.country_code,
                    publisher=body.publisher,
                    language=body.language,
                    document_type=body.document_type,
                    entry_url=body.entry_url,
                    strategy=body.strategy.value,
                    config=body.config.model_dump(),
                    interval_hours=body.interval_hours,
                    terms_url=body.terms_url,
                    terms_note=raw.get("note"),
                )
            )
            created += 1
        await session.commit()
    return created, skipped


async def preflight(country: str | None, sf=None) -> list[dict[str, str]]:
    """对库里的站点做只读预检(只访问各源站的 robots.txt,不下载任何文件)。"""
    from sqlalchemy import select

    from app.ingestion.fetcher import PoliteFetcher
    from app.sources.models import SourceSite

    settings = get_settings()
    async with (sf or get_sessionmaker())() as session:
        stmt = select(SourceSite).order_by(SourceSite.country_code, SourceSite.publisher)
        if country:
            stmt = stmt.where(SourceSite.country_code == country.upper())
        sites = (await session.execute(stmt)).scalars().all()

    rows: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=settings.crawl_timeout_seconds) as client:
        fetcher = PoliteFetcher(
            client,
            user_agent=settings.crawl_user_agent,
            delay_seconds=settings.crawl_delay_seconds,
            max_bytes=settings.crawl_max_bytes,
            allow_resolved_cidrs=settings.crawl_allow_resolved_cidrs,
        )
        for site in sites:
            res = await fetcher.preflight(site.entry_url)
            res["country"] = site.country_code
            res["publisher"] = site.publisher
            res["terms"] = site.terms_status
            rows.append(res)
    return rows


async def run_import_phase1(data_dir: str, country: str) -> None:
    from app.ingestion.archive import ArchiveStore
    from app.ingestion.phase1_import import import_phase1

    settings = get_settings()
    report = await import_phase1(
        Path(data_dir),
        country,
        get_sessionmaker(),
        ArchiveStore(settings.archive_dir),
        _ocr_engine(),
    )
    print(
        f"新增站点 {report.sites_new},新增文档 {report.documents_new},"
        f"新增版本 {report.versions_new},"
        f"已存在 {report.already_present}。解析结果: {dict(report.states)}"
    )
    if report.no_version:
        print(f"一期登记但没有原文的来源 {len(report.no_version)} 个(未导入)。")
    if report.missing_files:
        print(f"原文件缺失 {len(report.missing_files)} 个: {report.missing_files[:5]}")
    if report.hash_mismatch:
        print(f"哈希不一致已拒绝 {len(report.hash_mismatch)} 个: {report.hash_mismatch[:5]}")


def _ocr_engine():
    from app.ingestion.ocr import OcrEngine

    s = get_settings()
    return OcrEngine(s.tesseract_cmd, s.tessdata_dir, s.ocr_dpi)


async def reparse(state: str, country: str | None, sf=None) -> dict[str, int]:
    """对已有版本重新解析(例如装好 OCR 后,处理之前标为"需要 OCR"的扫描件)。"""
    from sqlalchemy import select

    from app.ingestion.archive import ArchiveStore
    from app.ingestion.models import Document, DocumentVersion
    from app.ingestion.pipeline import parse_version

    settings = get_settings()
    archive = ArchiveStore(settings.archive_dir)
    ocr = _ocr_engine()
    sf = sf or get_sessionmaker()
    counts: dict[str, int] = {}
    async with sf() as s:
        stmt = (
            select(DocumentVersion, Document)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.state == state)
        )
        if country:
            stmt = stmt.where(Document.country_code == country.upper())
        for version, doc in (await s.execute(stmt)).all():
            await parse_version(s, archive, version, doc.language, ocr)
            await s.commit()
            counts[version.state] = counts.get(version.state, 0) + 1
    return counts


def render_report(rows: list[dict[str, str]]) -> str:
    lines = [
        "# 来源预检报告(只读)",
        "",
        f"生成时间:{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}。",
        "只访问了各源站的 `robots.txt`,**没有下载任何法规文件**。",
        "「robots 允许」只说明技术上不被禁止,**不等于**条款允许商用或再分发,仍需法务确认。",
        "",
        "| 国家 | 发布机构 | 地址 | 地址检查 | robots | 说明 | 条款状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['country']} | {r['publisher']} | {r['url']} | {r['target']} | "
            f"{r['robots']} | {r['robots_detail']} | {r['terms']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-platform-admin", help="创建平台管理员")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="平台管理员")
    s = sub.add_parser("load-sites", help="导入种子站点(停用、条款待确认)")
    s.add_argument("file")
    r = sub.add_parser("reparse", help="重新解析已有版本(如补做 OCR)")
    r.add_argument("--state", default="needs_ocr")
    r.add_argument("--country")
    i = sub.add_parser("import-phase1", help="从一期迁移已有原文与元数据")
    i.add_argument("--data-dir", required=True, help="一期 docs/phase2/data 目录")
    i.add_argument("--country", required=True)
    f = sub.add_parser("preflight", help="只读预检站点(robots.txt 与地址)")
    f.add_argument("--country")
    f.add_argument("--report", help="把结果写成 Markdown 报告")
    args = parser.parse_args()

    if args.command == "create-platform-admin":
        password = os.environ.get("CYSH_ADMIN_PASSWORD") or getpass.getpass("密码(至少12位): ")
        if len(password) < 12:
            print("密码至少 12 位", file=sys.stderr)
            return 2
        asyncio.run(_create_platform_admin(args.email, args.name, password))
    elif args.command == "load-sites":
        created, skipped = asyncio.run(load_sites(Path(args.file)))
        print(f"新增 {created} 个站点,跳过已存在 {skipped} 个。新增站点均为停用、条款待确认。")
    elif args.command == "reparse":
        print("重新解析结果:", asyncio.run(reparse(args.state, args.country)))
    elif args.command == "import-phase1":
        asyncio.run(run_import_phase1(args.data_dir, args.country))
    elif args.command == "preflight":
        rows = asyncio.run(preflight(args.country))
        report = render_report(rows)
        if args.report:
            Path(args.report).write_text(report, encoding="utf-8")
            print(f"已写入 {args.report}")
        else:
            print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
