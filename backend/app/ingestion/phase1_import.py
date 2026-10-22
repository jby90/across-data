"""从一期平台迁移已有的官方原文与元数据(只迁移数据,不拷贝一期源码)。

读取一期 docs/phase2/data 下的 current/*.json 与 <国家>/<sha256>.pdf:
- 每份原文先校验 SHA-256 与文件名一致,再写入新平台的归档;
- 来源、标题、发布日期、版本说明等元数据照搬,一期没有的字段保持为空,不猜;
- 条款一律用新平台的解析器重新切分,不搬一期的条款或模型抽取结果;
- 现行状态一律记为 unknown(一期的 OFFICIAL_DATED_TEXT 只说明文本带官方日期,不等于现行有效)。
可重复执行:同一份原文(同文档同哈希)不会重复导入。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.archive import ArchiveStore
from app.ingestion.discovery import DEFAULT_EGAZETTE_QUERIES
from app.ingestion.models import Document, DocumentVersion, LegalStatus, PipelineEvent
from app.ingestion.ocr import OcrEngine
from app.ingestion.pipeline import parse_version
from app.sources.models import SourceSite, Strategy

log = logging.getLogger(__name__)

_PHASE1_TZ = timezone(timedelta(hours=8))  # 一期数据库时区 Asia/Shanghai
_TYPE_MAP = {
    "ACT": "LAW",
    "REVISED_ACT": "LAW",
    "LAW": "LAW",
    "REGULATION": "REGULATION",
    "REGULATOR_GUIDANCE": "GUIDANCE",
    "HISTORICAL_GUIDANCE": "GUIDANCE",
    "ADMISSION_GUIDANCE": "GUIDANCE",
    "GUIDANCE": "GUIDANCE",
    "FORM": "FORM",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EGAZETTE_INDEX_URL = "https://www.egazette.gov.sg/egazette-browse/"

# 一期已落盘校验过、但未汇总进 current/sources.json 的其余四国官方 PDF
_EXTRA_PHASE1_SOURCES: list[dict] = [
    {
        "id": "extra-eg-iot",
        "country_code": "EG",
        "publisher": "埃及国家电信监管局 NTRA",
        "language": "ar",
        "document_type": "GUIDANCE",
        "source_url": "https://www.tra.gov.eg/wp-content/uploads/2023/08/IoT-Framework-AR.pdf",
        "title": (
            "الإطار التنظيمي الخاص بتقديم خدمات انترنت الأشياء في جمهورية مصر العربية"
            " — Internet of Things (IoT) Framework"
        ),
        "content_hash": "11566a8620fac916e49a0c3362f1324fb68069832317b48d6cf97cacfcf1edd7",
        "published_date": "2022-04-01",
    },
    {
        "id": "extra-tn-loi63-fr",
        "country_code": "TN",
        "publisher": "突尼斯国家统计局 INS",
        "language": "fr",
        "document_type": "LAW",
        "source_url": (
            "https://www.ins.tn/sites/default/files-ftp3/files/2020-04/Loi%2063-2004%20Fr.pdf"
        ),
        "title": (
            "Loi organique n° 2004-63 du 27 juillet 2004 portant sur la protection "
            "des données à caractère personnel (texte français)"
        ),
        "content_hash": "398b4b3499ff0449f86cf4a86eac84421121e1701689c5d395557111eeca5d77",
        "published_date": "2004-07-27",
    },
    {
        "id": "extra-tn-loi63-ar",
        "country_code": "TN",
        "publisher": "突尼斯司法部",
        "language": "ar",
        "document_type": "LAW",
        "source_url": (
            "https://www.justice.gov.tn/fileadmin/medias/"
            "organisation_juridictionelle/Loi2004_63Arabe.pdf"
        ),
        "title": (
            "قانون أساسي عدد 63 لسنة 2004 مؤرخ في 27 جويلية 2004 يتعلق بحماية المعطيات الشخصية"
        ),
        "content_hash": "7b12a8ddddba535f4afb61ab95f9f9be411fdecc31cda1e9242145853edf2735",
        "published_date": "2004-07-27",
    },
    {
        "id": "extra-ma-loi0908",
        "country_code": "MA",
        "publisher": "摩洛哥国家个人数据保护委员会 CNDP",
        "language": "fr",
        "document_type": "LAW",
        "source_url": "https://www.cndp.ma/wp-content/uploads/2023/11/Loi-09-08-Fr.pdf",
        "title": (
            "Loi n° 09-08 relative à la protection des personnes physiques à l’égard "
            "du traitement des données à caractère personnel"
        ),
        "content_hash": "4b5e910545336cc9d66e0a4f9af648f1b10f1c9dfb97059e610751783d2ad7ed",
        "published_date": "2009-02-18",
    },
    {
        "id": "extra-ma-decret209165",
        "country_code": "MA",
        "publisher": "摩洛哥国家个人数据保护委员会 CNDP",
        "language": "fr",
        "document_type": "REGULATION",
        "source_url": "https://www.cndp.ma/wp-content/uploads/2023/06/Decret-2-09-165-FR.pdf",
        "title": "Décret n° 2-09-165 pris pour l'application de la loi n° 09-08",
        "content_hash": "0a930a84d692c5b7e82a328970c52f10c52ee55ba75bb974918f0694a901365b",
        "published_date": "2009-05-18",
    },
    {
        "id": "extra-ma-cdai-form",
        "country_code": "MA",
        "publisher": "摩洛哥国家个人数据保护委员会 CNDP",
        "language": "fr",
        "document_type": "FORM",
        "source_url": (
            "https://www.cndp.ma/wp-content/uploads/2023/11/"
            "CDAI-FORMULAIRE-DAI-Template-FRANCAIS.pdf"
        ),
        "title": "Formulaire de demande d'accès à l'information / déclaration CNDP",
        "content_hash": "37710bb33ae3a3f450dfcb85e80f4998bffaa9909ab9aa8be4e48ee78e8ad58c",
    },
    {
        "id": "extra-lb-law81",
        "country_code": "LB",
        "publisher": "黎巴嫩中央银行 BDL(官方托管)",
        "language": "ar",
        "document_type": "LAW",
        "source_url": (
            "https://www.bdl.gov.lb/CB%20Com/Laws%20And%20Regulations/"
            "Laws/Law_81_AR%C2%A72572_1.pdf"
        ),
        "title": (
            "قانون رقم 81 تاريخ 10/10/2018 — المعاملات الإلكترونية والبيانات ذات الطابع الشخصي"
        ),
        "content_hash": "7c13a15bbbdedd928d6edde63f306324221f989deed3f849d6f6638c691c98af",
        "published_date": "2018-10-10",
    },
]


@dataclass
class ImportReport:
    sites_new: int = 0
    documents_new: int = 0
    versions_new: int = 0
    already_present: int = 0
    no_version: list[str] = field(default_factory=list)  # 一期登记了来源但没有采集到原文
    missing_files: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)
    states: Counter = field(default_factory=Counter)


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


async def consolidate_egazette_sites(session: AsyncSession) -> int:
    """将旧版本按单份 PDF 拆成多行的电子宪报站点合并到统一的检索目录站点。"""
    legacy = list(
        (
            await session.execute(
                select(SourceSite).where(
                    SourceSite.country_code == "SG",
                    SourceSite.entry_url.like("https://assets.egazette.gov.sg/%"),
                )
            )
        )
        .scalars()
        .all()
    )
    if legacy:
        canonical = (
            await session.execute(
                select(SourceSite).where(
                    SourceSite.country_code == "SG",
                    SourceSite.entry_url == EGAZETTE_INDEX_URL,
                )
            )
        ).scalar_one_or_none()
        if canonical is None:
            canonical = SourceSite(
                country_code="SG",
                publisher="Singapore Government Gazette",
                language="en",
                document_type="LAW",
                entry_url=EGAZETTE_INDEX_URL,
                strategy=Strategy.EGAZETTE.value,
                config={"keywords": list(DEFAULT_EGAZETTE_QUERIES), "extensions": [".pdf"]},
                terms_note="新加坡电子宪报官方检索目录(按关键词自动发现立法增刊 PDF)",
            )
            session.add(canonical)
            await session.flush()
        for old_site in legacy:
            docs = (
                (await session.execute(select(Document).where(Document.site_id == old_site.id)))
                .scalars()
                .all()
            )
            for d in docs:
                dup = (
                    await session.execute(
                        select(Document).where(
                            Document.site_id == canonical.id,
                            Document.source_url == d.source_url,
                        )
                    )
                ).scalar_one_or_none()
                if dup is None:
                    d.site_id = canonical.id
            await session.flush()
            await session.delete(old_site)
        await session.commit()
    return len(legacy)


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value and _DATE.match(value) else None


def _retrieved(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=_PHASE1_TZ).astimezone(UTC)
    except ValueError:
        return None


def _title(source: dict, meta: dict | None) -> str:
    if meta and meta.get("metadata_json"):
        try:
            title = json.loads(meta["metadata_json"]).get("title")
            if title:
                return str(title)[:500]
        except (ValueError, AttributeError):
            pass
    return str(source.get("title") or source["source_url"])[:500]


def _site_spec(country: str, source: dict, meta: dict | None) -> tuple[str, str, dict, str]:
    """决定某份来源文件归属哪个站点:返回 (entry_url, strategy, config, terms_note)。

    凡是通过官方目录发现的来源,全部归入同一个目录站点,不再按每份 PDF 建一行站点。"""
    index_url = (meta or {}).get("official_index_url")
    host = urlsplit(source["source_url"]).hostname or ""
    if index_url == EGAZETTE_INDEX_URL or host == "assets.egazette.gov.sg":
        return (
            EGAZETTE_INDEX_URL,
            Strategy.EGAZETTE.value,
            {"keywords": list(DEFAULT_EGAZETTE_QUERIES), "extensions": [".pdf"]},
            "新加坡电子宪报官方检索目录(按关键词自动发现立法增刊 PDF)",
        )
    if index_url:
        return (
            str(index_url),
            Strategy.INDEX_PAGE.value,
            {"extensions": [".pdf"]},
            "一期迁移(目录站点)",
        )
    return (
        source["source_url"],
        Strategy.DIRECT.value,
        {},
        f"一期迁移:{_title(source, meta)[:120]}",
    )


async def import_phase1(
    data_dir: Path,
    country: str,
    sf: async_sessionmaker[AsyncSession],
    archive: ArchiveStore,
    ocr: OcrEngine | None = None,
) -> ImportReport:
    country = country.upper()
    current = data_dir / "current"
    sources = (
        [s for s in _load(current / "sources.json") if s["country_code"] == country]
        if (current / "sources.json").exists()
        else []
    )
    versions: dict[str, list[dict]] = {}
    if (current / "versions.json").exists():
        for v in sorted(_load(current / "versions.json"), key=lambda x: int(x["id"])):
            versions.setdefault(v["source_id"], []).append(v)
    metas = (
        {m["source_id"]: m for m in _load(current / "source-metadata.json")}
        if (current / "source-metadata.json").exists()
        else {}
    )
    existing_urls = {s["source_url"] for s in sources}
    for extra in _EXTRA_PHASE1_SOURCES:
        if extra["country_code"] == country and extra["source_url"] not in existing_urls:
            pdf_path = data_dir / country / f"{extra['content_hash']}.pdf"
            if pdf_path.exists():
                sources.append(extra)
                versions[extra["id"]] = [
                    {
                        "id": extra["id"],
                        "source_id": extra["id"],
                        "content_hash": extra["content_hash"],
                        "published_date": extra.get("published_date"),
                    }
                ]

    report = ImportReport()
    async with sf() as s:
        await consolidate_egazette_sites(s)
    for source in sources:
        vers = versions.get(source["id"], [])
        if not vers:
            report.no_version.append(source["source_url"])
            continue
        meta = metas.get(source["id"])
        entry_url, strategy, cfg, note = _site_spec(country, source, meta)
        doc_type = _TYPE_MAP.get(source["document_type"], "OTHER")
        async with sf() as s:
            site = (
                await s.execute(
                    select(SourceSite).where(
                        SourceSite.country_code == country,
                        SourceSite.entry_url == entry_url,
                    )
                )
            ).scalar_one_or_none()
            if site is None:
                site = SourceSite(
                    country_code=country,
                    publisher=source["publisher"],
                    language=source["language"],
                    document_type="LAW" if strategy == Strategy.EGAZETTE.value else doc_type,
                    entry_url=entry_url,
                    strategy=strategy,
                    config=cfg,
                    terms_note=note,
                )
                s.add(site)
                await s.flush()
                report.sites_new += 1

            doc = (
                await s.execute(
                    select(Document).where(
                        Document.site_id == site.id, Document.source_url == source["source_url"]
                    )
                )
            ).scalar_one_or_none()
            if doc is None:
                doc = Document(
                    site_id=site.id,
                    country_code=country,
                    source_url=source["source_url"],
                    title=_title(source, meta),
                    language=source["language"],
                    document_type=doc_type,
                    instrument_family=(meta or {}).get("instrument_family"),
                    instrument_kind=(meta or {}).get("instrument_kind") or source["document_type"],
                )
                s.add(doc)
                await s.flush()
                report.documents_new += 1

            for v in vers:
                digest = v["content_hash"]
                exists = (
                    await s.execute(
                        select(DocumentVersion.id).where(
                            DocumentVersion.document_id == doc.id,
                            DocumentVersion.content_hash == digest,
                        )
                    )
                ).first()
                if exists:
                    report.already_present += 1
                    continue
                path = data_dir / country / f"{digest}.pdf"
                if not path.exists():
                    report.missing_files.append(str(path.name))
                    continue
                content = path.read_bytes()
                if ArchiveStore.sha256(content) != digest:
                    report.hash_mismatch.append(path.name)
                    continue
                stored_digest, rel = archive.put(content)
                last_no = (
                    await s.execute(
                        select(DocumentVersion.version_no)
                        .where(DocumentVersion.document_id == doc.id)
                        .order_by(DocumentVersion.version_no.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                version = DocumentVersion(
                    document_id=doc.id,
                    version_no=(last_no or 0) + 1,
                    content_hash=stored_digest,
                    archive_path=rel,
                    size_bytes=len(content),
                    mime_type="application/pdf",
                    final_url=source["source_url"][:2048],
                    fetched_at=_retrieved(v.get("retrieved_at")) or datetime.now(UTC),
                    official_number=v.get("official_number"),
                    version_label=(v.get("official_version_label") or None),
                    published_date=_d(v.get("published_date"))
                    or _d((meta or {}).get("published_date")),
                    effective_date=_d(v.get("effective_date")),
                    repealed_date=_d(v.get("repealed_date")),
                    legal_status=LegalStatus.UNKNOWN.value,
                    metadata_source="phase1_import",
                )
                s.add(version)
                await s.flush()
                s.add(
                    PipelineEvent(
                        version_id=version.id,
                        step="import",
                        status="ok",
                        message=f"从一期迁移,sha256={digest[:12]},原文件已校验",
                    )
                )
                report.versions_new += 1
                await parse_version(s, archive, version, source["language"], ocr)
                report.states[version.state] += 1
            await s.commit()
    return report
