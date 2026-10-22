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
import hashlib
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
    """将历史遗留的超长未切分条款按句/款切分为标准条款,并保持 KgEdge 引用完整性。"""
    from sqlalchemy import func, select

    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.knowledge.models import KgEdge
    from app.knowledge.translator import split_clause_into_chunks

    stmt = (
        select(Clause, DocumentVersion, Document)
        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(func.length(Clause.original_text) > max_clause_chars)
    )
    if country_code:
        stmt = stmt.where(Document.country_code == country_code.upper())

    rows = list((await session.execute(stmt)).all())
    split_count = 0
    for clause, ver, _doc in rows:
        chunks = split_clause_into_chunks(clause.original_text, max_chars=1400)
        if len(chunks) <= 1:
            continue
        max_ord = (
            await session.execute(
                select(func.max(Clause.ordinal)).where(Clause.version_id == ver.id)
            )
        ).scalar_one() or 0

        edges = list(
            (await session.execute(select(KgEdge).where(KgEdge.evidence_clause_id == clause.id)))
            .scalars()
            .all()
        )
        clause.original_text = chunks[0]
        clause.text_hash = hashlib.sha256(chunks[0].encode("utf-8")).hexdigest()

        created_clauses: list[Clause] = [clause]
        for idx, ch_text in enumerate(chunks[1:], start=1):
            new_c = Clause(
                version_id=ver.id,
                ordinal=max_ord + idx,
                clause_number=f"{clause.clause_number or clause.ordinal}.{idx}",
                heading=clause.heading,
                section_path=clause.section_path,
                language=clause.language,
                original_text=ch_text,
                text_hash=hashlib.sha256(ch_text.encode("utf-8")).hexdigest(),
                page_start=clause.page_start,
                page_end=clause.page_end,
                ocr_required=clause.ocr_required,
                needs_review=clause.needs_review,
            )
            session.add(new_c)
            created_clauses.append(new_c)
        await session.flush()

        for edge in edges:
            if edge.evidence_quote and edge.evidence_quote in clause.original_text:
                continue
            matched_c = next(
                (
                    nc
                    for nc in created_clauses
                    if edge.evidence_quote and edge.evidence_quote in nc.original_text
                ),
                clause,
            )
            if matched_c.id != clause.id:
                edge.evidence_clause_id = matched_c.id
            elif edge.evidence_quote not in clause.original_text:
                edge.evidence_quote = clause.original_text[:240].strip()

        split_count += 1
    if split_count:
        await session.commit()
    return split_count


async def batch_translate(
    country: str | None = None,
    only_missing: bool = True,
    concurrency: int = 4,
    sf=None,
) -> dict[str, int]:
    """批量执行受术语表约束的真实大模型翻译(S1):
    1. 将已有的非 llm (glossary_assisted) 译文全部标记为 superseded(保留历史行);
    2. 按国家加载术语库,并发调用 LLM 翻译目标条款并执行四项译后检查。
    """
    from sqlalchemy import or_, select

    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.knowledge.models import (
        ClauseTranslation,
        GlossaryTerm,
        KgEdge,
        ReviewPoint,
        TranslationReviewStatus,
    )
    from app.knowledge.translator import set_translation_concurrency, translate_clause

    set_translation_concurrency(concurrency)
    sf = sf or get_sessionmaker()

    async with sf() as s:
        await _split_oversized_clauses_for_country(s, country_code=country)

        # 1. 作废所有旧的 glossary_assisted 记录(标为 superseded)
        old_stmt = (
            select(ClauseTranslation, Clause, Document)
            .join(Clause, Clause.id == ClauseTranslation.clause_id)
            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                ClauseTranslation.engine != "llm",
                ClauseTranslation.review_status != TranslationReviewStatus.SUPERSEDED.value,
            )
        )
        if country:
            old_stmt = old_stmt.where(Document.country_code == country.upper())
        old_rows = list((await s.execute(old_stmt)).all())
        for tr, _cl, _doc in old_rows:
            tr.review_status = TranslationReviewStatus.SUPERSEDED.value
            tr.updated_at = datetime.now(UTC)
        await s.commit()

        # 2. 收集需要翻译的目标条款集合:
        #    (a) 原本已有译文(含刚标为 superseded)或处于 failed/not_translated 的条款;
        #    (b) 知识图谱 KgEdge 或 ReviewPoint 引用的证据条款;
        #    (c) 各相关文档最新已解析版本的核心条款(SG 前 8 条,其他四国前 15 条)。
        target_countries = [country.upper()] if country else ["SG", "EG", "TN", "MA", "LB"]
        clauses_to_run: list[tuple[Clause, str]] = []
        seen_cids: set = set()

        for cc in target_countries:
            done_cids: set = set()
            if only_missing:
                done_rows = (
                    (
                        await s.execute(
                            select(ClauseTranslation.clause_id)
                            .join(Clause, Clause.id == ClauseTranslation.clause_id)
                            .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                            .join(Document, Document.id == DocumentVersion.document_id)
                            .where(
                                Document.country_code == cc,
                                ClauseTranslation.engine == "llm",
                                ClauseTranslation.review_status.in_(
                                    (
                                        TranslationReviewStatus.MACHINE_TRANSLATED.value,
                                        TranslationReviewStatus.PENDING_REVIEW.value,
                                        TranslationReviewStatus.APPROVED.value,
                                    )
                                ),
                                ClauseTranslation.quality_check_passed.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                done_cids = set(done_rows)

            tr_cids = set(
                (
                    await s.execute(
                        select(ClauseTranslation.clause_id)
                        .join(Clause, Clause.id == ClauseTranslation.clause_id)
                        .join(DocumentVersion, DocumentVersion.id == Clause.version_id)
                        .join(Document, Document.id == DocumentVersion.document_id)
                        .where(Document.country_code == cc)
                    )
                )
                .scalars()
                .all()
            )
            edge_cids = set(
                (
                    await s.execute(
                        select(KgEdge.evidence_clause_id).where(KgEdge.country_code == cc)
                    )
                )
                .scalars()
                .all()
            )
            rp_cids = set(
                (
                    await s.execute(
                        select(ReviewPoint.clause_id).where(
                            ReviewPoint.country_code == cc, ReviewPoint.clause_id.is_not(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            must_cids = tr_cids | edge_cids | rp_cids

            sample_n = 8 if cc == "SG" else 15
            docs = list(
                (
                    await s.execute(
                        select(Document).where(
                            Document.country_code == cc,
                            Document.relevance == "relevant",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for doc in docs:
                ver = (
                    await s.execute(
                        select(DocumentVersion)
                        .where(
                            DocumentVersion.document_id == doc.id,
                            DocumentVersion.state == "parsed",
                        )
                        .order_by(DocumentVersion.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if ver is None:
                    continue
                doc_cids = (
                    (
                        await s.execute(
                            select(Clause.id)
                            .where(Clause.version_id == ver.id)
                            .order_by(Clause.ordinal)
                            .limit(sample_n)
                        )
                    )
                    .scalars()
                    .all()
                )
                must_cids.update(doc_cids)

            pending_cids = [
                cid for cid in must_cids if cid and cid not in done_cids and cid not in seen_cids
            ]
            if pending_cids:
                c_objs = list(
                    (
                        await s.execute(
                            select(Clause)
                            .where(Clause.id.in_(pending_cids))
                            .order_by(Clause.ordinal)
                        )
                    )
                    .scalars()
                    .all()
                )
                for c_obj in c_objs:
                    seen_cids.add(c_obj.id)
                    clauses_to_run.append((c_obj, cc))

    print(
        f"[translate] 已作废旧译文 {len(old_rows)} 条; "
        f"待翻译条款 {len(clauses_to_run)} 条 (concurrency={concurrency})",
        flush=True,
    )

    terms_by_country: dict[str, list[GlossaryTerm]] = {}
    async with sf() as s:
        for cc in {cc for _, cc in clauses_to_run}:
            terms_by_country[cc] = list(
                (
                    await s.execute(
                        select(GlossaryTerm).where(
                            or_(
                                GlossaryTerm.country_code == cc,
                                GlossaryTerm.country_code.is_(None),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )

    sem = asyncio.Semaphore(max(1, concurrency))
    stats = {
        "superseded": len(old_rows),
        "total": len(clauses_to_run),
        "machine_translated": 0,
        "translation_failed": 0,
        "not_translated": 0,
    }
    done_counter = 0

    async def _worker(clause_obj: Clause, cc: str) -> None:
        nonlocal done_counter
        async with sem:
            async with sf() as task_s:
                c_in_s = await task_s.get(Clause, clause_obj.id)
                if c_in_s is None:
                    return
                tr = await translate_clause(task_s, c_in_s, cc, terms_by_country.get(cc, []))
                await task_s.commit()
                st = tr.review_status
                stats[st] = stats.get(st, 0) + 1
                done_counter += 1
                if done_counter % 20 == 0 or done_counter == len(clauses_to_run):
                    print(
                        f"  进度 {done_counter}/{len(clauses_to_run)} | "
                        f"成功={stats.get('machine_translated', 0)} "
                        f"失败={stats.get('translation_failed', 0)} "
                        f"未译={stats.get('not_translated', 0)}",
                        flush=True,
                    )

    await asyncio.gather(*(_worker(c_obj, cc) for c_obj, cc in clauses_to_run))
    return stats


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
    t.add_argument("--concurrency", type=int, default=4, help="并发请求数(默认 4)")
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
            )
        )
        print("批量翻译结果:", json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
