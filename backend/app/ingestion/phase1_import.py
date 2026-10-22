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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Document, DocumentVersion, LegalStatus, PipelineEvent
from app.ingestion.ocr import OcrEngine
from app.ingestion.pipeline import parse_version
from app.sources.models import SourceSite, Strategy

log = logging.getLogger(__name__)

_PHASE1_TZ = timezone(timedelta(hours=8))  # 一期数据库时区 Asia/Shanghai
_TYPE_MAP = {
    "ACT": "LAW",
    "REVISED_ACT": "LAW",
    "REGULATION": "REGULATION",
    "REGULATOR_GUIDANCE": "GUIDANCE",
    "HISTORICAL_GUIDANCE": "GUIDANCE",
    "ADMISSION_GUIDANCE": "GUIDANCE",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


async def import_phase1(
    data_dir: Path,
    country: str,
    sf: async_sessionmaker[AsyncSession],
    archive: ArchiveStore,
    ocr: OcrEngine | None = None,
) -> ImportReport:
    country = country.upper()
    current = data_dir / "current"
    sources = [s for s in _load(current / "sources.json") if s["country_code"] == country]
    versions: dict[str, list[dict]] = {}
    for v in sorted(_load(current / "versions.json"), key=lambda x: int(x["id"])):
        versions.setdefault(v["source_id"], []).append(v)
    metas = {m["source_id"]: m for m in _load(current / "source-metadata.json")}

    report = ImportReport()
    for source in sources:
        vers = versions.get(source["id"], [])
        if not vers:
            report.no_version.append(source["source_url"])
            continue
        meta = metas.get(source["id"])
        async with sf() as s:
            site = (
                await s.execute(
                    select(SourceSite).where(
                        SourceSite.country_code == country,
                        SourceSite.entry_url == source["source_url"],
                    )
                )
            ).scalar_one_or_none()
            if site is None:
                site = SourceSite(
                    country_code=country,
                    publisher=source["publisher"],
                    language=source["language"],
                    document_type=_TYPE_MAP.get(source["document_type"], "OTHER"),
                    entry_url=source["source_url"],
                    strategy=Strategy.DIRECT.value,
                    config={},
                    terms_note="一期迁移",
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
                    document_type=site.document_type,
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
