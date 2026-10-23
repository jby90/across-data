"""命令行工具。

用法:
  python -m app.cli create-platform-admin --email admin@example.com --name 管理员
  python -m app.cli load-sites ../data/seed/source_sites.json   # 导入种子站点(停用、条款待确认)
  python -m app.cli preflight [--country EG] [--report 报告.md]  # 只读预检,不下载文件
  python -m app.cli translate [--country SG] [--only-missing] [--concurrency 4]

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


def _ensure_dev_env() -> None:
    """当未显式传入 CYSH_DATABASE_URL / CYSH_JWT_SECRET 时,自动复用 ~/.cyshuhe-dev 本地开发配置。"""
    dev_dir = Path.home() / ".cyshuhe-dev"
    creds_path = dev_dir / "credentials.json"
    if "CYSH_JWT_SECRET" not in os.environ and creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            if creds.get("jwt_secret"):
                os.environ["CYSH_JWT_SECRET"] = creds["jwt_secret"]
        except Exception:
            pass
    if "CYSH_JWT_SECRET" not in os.environ:
        os.environ["CYSH_JWT_SECRET"] = "dev-cli-fallback-jwt-secret-at-least-32-chars"

    if "CYSH_DATABASE_URL" not in os.environ and (dev_dir / "pg").exists():
        try:
            import pgserver

            pg = pgserver.get_server(dev_dir / "pg", cleanup_mode=None)
            os.environ["CYSH_DATABASE_URL"] = pg.get_uri().replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        except Exception:
            pass
    get_settings.cache_clear()


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


async def _split_oversized_clauses_for_country(
    session, country_code: str | None = None, max_clause_chars: int = 2400
) -> int:
    """Compatibility entry point: translation chunks must never modify source clauses.

    Chunking happens in translator.split_clause_into_chunks in memory only.
    Historical repairs require separate reviewed parse revisions.
    """
    return 0


async def batch_translate(
    country: str | None = None,
    only_missing: bool = True,
    concurrency: int = 4,
    sf=None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Translate every latest parsed clause; explicit limits are samples, never full coverage.

    Dry runs do not write. Successful attempts commit independently, allowing safe resume.
    All source/evidence rows and previous translation attempt contents remain untouched.
    """
    from sqlalchemy import func, or_, select

    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.knowledge.models import ClauseTranslation, GlossaryTerm
    from app.knowledge.translator import (
        set_translation_concurrency,
        translate_clause,
        verify_translation,
    )

    set_translation_concurrency(concurrency)
    sf = sf or get_sessionmaker()
    target_countries = [country.upper()] if country else ["SG", "EG", "TN", "MA", "LB"]
    async with sf() as session:
        latest = (
            select(
                DocumentVersion.document_id,
                func.max(DocumentVersion.version_no).label("version_no"),
            )
            .where(DocumentVersion.state == "parsed")
            .group_by(DocumentVersion.document_id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(Clause, Document.country_code)
                .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(
                    latest,
                    (latest.c.document_id == Document.id)
                    & (latest.c.version_no == DocumentVersion.version_no),
                )
                .where(Document.country_code.in_(target_countries))
                .order_by(Document.country_code, Document.id, Clause.ordinal, Clause.id)
            )
        ).all()
        current = (
            await session.scalars(
                select(ClauseTranslation).where(
                    ClauseTranslation.is_current.is_(True),
                    ClauseTranslation.review_status.in_(
                        ("machine_translated", "pending_review", "approved")
                    ),
                )
            )
        ).all()
        qualified = set()
        clauses_by_id = {clause.id: clause for clause, _ in rows}
        for tr in current:
            clause = clauses_by_id.get(tr.clause_id)
            if clause and (
                tr.review_status == "approved"
                or (
                    tr.engine == "llm"
                    and tr.quality_check_passed
                    and verify_translation(
                        clause.original_text, tr.translated_text, tr.glossary_hits or []
                    )[0]
                )
            ):
                qualified.add(tr.clause_id)
        pending = [
            (clause.id, cc) for clause, cc in rows if not only_missing or clause.id not in qualified
        ]
        total_missing = len(pending)
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            pending = pending[:limit]
        terms = (
            await session.scalars(
                select(GlossaryTerm).where(
                    or_(
                        GlossaryTerm.country_code.in_(target_countries),
                        GlossaryTerm.country_code.is_(None),
                    )
                )
            )
        ).all()
    stats = dict(
        total_eligible=len(rows),
        total_missing=total_missing,
        total=len(pending),
        skipped_qualified=len(rows) - total_missing,
        limited=int(limit is not None),
        machine_translated=0,
        translation_failed=0,
        not_translated=0,
        errors=0,
        dry_run=int(dry_run),
        superseded=0,
        tokens_in=0,
        tokens_out=0,
    )
    print("[translate] " + json.dumps(stats, ensure_ascii=False), flush=True)
    if dry_run:
        return stats
    sem = asyncio.Semaphore(max(1, concurrency))
    completed = 0

    async def worker(clause_id, cc):
        nonlocal completed
        async with sem:
            async with sf() as session:
                try:
                    clause = await session.get(Clause, clause_id)
                    if clause is None:
                        stats["errors"] += 1
                        return
                    tr = await translate_clause(session, clause, cc, terms)
                    await session.commit()
                    stats[tr.review_status] = stats.get(tr.review_status, 0) + 1
                    if tr.review_status != "approved":
                        stats["tokens_in"] += tr.tokens_in
                        stats["tokens_out"] += tr.tokens_out
                    completed += 1
                    if completed % 20 == 0 or completed == len(pending):
                        print(
                            f"[translate] {completed}/{len(pending)} "
                            + json.dumps(stats, ensure_ascii=False),
                            flush=True,
                        )
                except Exception:
                    await session.rollback()
                    stats["errors"] += 1
                    # Never print request bodies, credentials, or untrusted exception payloads.
                    print(
                        f"[translate] clause {clause_id}: processing failed; safe to retry",
                        flush=True,
                    )

    await asyncio.gather(*(worker(clause_id, cc) for clause_id, cc in pending))
    return stats


async def batch_extract_kg(
    country: str | None = None,
    max_clauses_per_doc: int | None = None,
    concurrency: int = 8,
    sf=None,
) -> dict[str, dict[str, int]]:
    """批量执行 S2 大模型知识图谱抽取(相关性过滤 + 条款抽取 + 类型约束 + 逐字校验 + 语义裁判)。"""
    from app.knowledge.extractor import COUNTRY_NAMES, extract_knowledge_for_country

    sf = sf or get_sessionmaker()
    countries = [country.upper()] if country else list(COUNTRY_NAMES.keys())
    results: dict[str, dict[str, int]] = {}
    for cc in countries:
        print(f"[S2-KG] 正在抽取国家 {cc} ...", flush=True)
        async with sf() as session:
            res = await extract_knowledge_for_country(
                session,
                cc,
                translate_sample_per_doc=0,
                max_clauses_per_doc=max_clauses_per_doc,
                concurrency=concurrency,
            )
            results[cc] = res
            print(f"[S2-KG] {cc} 完成: {json.dumps(res, ensure_ascii=False)}", flush=True)
    return results


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
    t = sub.add_parser("translate", help="批量执行真实大模型受控法律翻译(S1)")
    t.add_argument("--country", help="指定国家代码(如 SG/EG/TN/MA/LB),不传则翻译全部五国")
    t.add_argument(
        "--only-missing",
        action="store_true",
        default=False,
        help="仅翻译缺少合格 LLM 译文的条款",
    )
    t.add_argument("--dry-run", action="store_true", help="只列出全量目标/缺口，不调用模型、不写库")
    t.add_argument("--limit", type=int, help="显式小样本上限；不传则全量")
    t.add_argument("--concurrency", type=int, default=4, help="并发请求数(默认 4)")
    kg = sub.add_parser("extract-kg", help="批量执行 S2 知识图谱结构化抽取与语义裁判")
    kg.add_argument("--country", help="指定国家代码(如 SG/EG/TN/MA/LB),不传则抽取全部五国")
    kg.add_argument(
        "--max-clauses",
        type=int,
        default=None,
        help="显式抽样时每份文档条款上限；不传则抽取全部范围内条款",
    )
    kg.add_argument("--concurrency", type=int, default=8, help="并发请求数(默认 8)")
    args = parser.parse_args()

    _ensure_dev_env()

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
    elif args.command == "translate":
        res = asyncio.run(
            batch_translate(
                country=args.country,
                only_missing=args.only_missing,
                concurrency=args.concurrency,
                dry_run=args.dry_run,
                limit=args.limit,
            )
        )
        print("批量翻译结果:", json.dumps(res, ensure_ascii=False))
    elif args.command == "extract-kg":
        res = asyncio.run(
            batch_extract_kg(
                country=args.country,
                max_clauses_per_doc=args.max_clauses,
                concurrency=args.concurrency,
            )
        )
        print("批量知识图谱抽取结果:", json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
