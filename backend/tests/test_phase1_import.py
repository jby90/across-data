import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion
from app.ingestion.phase1_import import import_phase1
from app.sources.models import SourceSite

from .helpers import make_pdf


def build_phase1_dir(tmp_path: Path):
    """造一个最小的一期数据目录。"""
    data = tmp_path / "phase1"
    (data / "current").mkdir(parents=True)
    (data / "SG").mkdir()

    good = make_pdf(
        [
            ["Article 1 Scope", "This Act applies to all vehicle data held by organisations."],
            ["Article 2 Terms", "Cross-border transfers need the approval of the Commission."],
        ]
    )
    other = make_pdf([["Rule 1 Alpha", "text"], ["Rule 2 Beta", "text"]])
    h_good, h_other = hashlib.sha256(good).hexdigest(), hashlib.sha256(other).hexdigest()
    (data / "SG" / f"{h_good}.pdf").write_bytes(good)
    (data / "SG" / f"{h_other}.pdf").write_bytes(b"corrupted, does not match its name")

    sources = [
        {
            "id": "sg-a",
            "country_code": "SG",
            "title": "A",
            "publisher": "机构A",
            "language": "en",
            "source_url": "https://example.org/a.pdf",
            "document_type": "ACT",
        },
        {
            "id": "sg-b",
            "country_code": "SG",
            "title": "B",
            "publisher": "机构B",
            "language": "en",
            "source_url": "https://example.org/b.pdf",
            "document_type": "REGULATION",
        },
        {
            "id": "sg-c",
            "country_code": "SG",
            "title": "C",
            "publisher": "机构C",
            "language": "en",
            "source_url": "https://example.org/c.pdf",
            "document_type": "REGULATOR_GUIDANCE",
        },
        {
            "id": "sg-d",
            "country_code": "SG",
            "title": "D(无原文)",
            "publisher": "机构D",
            "language": "en",
            "source_url": "https://example.org/d.pdf",
            "document_type": "ACT",
        },
        {
            "id": "eg-x",
            "country_code": "EG",
            "title": "X",
            "publisher": "埃及",
            "language": "ar",
            "source_url": "https://example.org/x.pdf",
            "document_type": "ACT",
        },
    ]
    versions = [
        {
            "id": "1",
            "source_id": "sg-a",
            "content_hash": h_good,
            "official_number": "Act 1 of 2012",
            "official_version_label": "Revised 2020",
            "published_date": "2012-10-15",
            "effective_date": "2014-07-02",
            "repealed_date": None,
            "legal_status": "OFFICIAL_DATED_TEXT",
            "retrieved_at": "2026-10-19 10:45:52",
        },
        {
            "id": "2",
            "source_id": "sg-b",
            "content_hash": h_other,
            "official_number": None,
            "official_version_label": None,
            "published_date": None,
            "effective_date": None,
            "repealed_date": None,
            "legal_status": "UNKNOWN",
            "retrieved_at": "2026-10-19 10:45:53",
        },
        {
            "id": "3",
            "source_id": "sg-c",
            "content_hash": "f" * 64,
            "official_number": None,
            "official_version_label": None,
            "published_date": None,
            "effective_date": None,
            "repealed_date": None,
            "legal_status": "UNKNOWN",
            "retrieved_at": "2026-10-19 10:45:54",
        },
    ]
    metas = [
        {
            "source_id": "sg-a",
            "instrument_family": "sg-pdpa",
            "instrument_kind": "ACT",
            "published_date": "2012-10-15",
            "metadata_json": json.dumps({"title": "Personal Data Protection Act 2012"}),
        },
    ]
    for name, obj in [("sources", sources), ("versions", versions), ("source-metadata", metas)]:
        (data / "current" / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")
    return data, good, h_good


async def test_import_maps_metadata_and_never_claims_in_force(session_factory, tmp_path):
    data, good, h_good = build_phase1_dir(tmp_path)
    archive = ArchiveStore(tmp_path / "archive")
    report = await import_phase1(data, "SG", session_factory, archive)

    assert report.versions_new == 1 and report.documents_new == 3 and report.sites_new == 3
    assert report.no_version == ["https://example.org/d.pdf"]
    assert report.hash_mismatch and report.missing_files == ["f" * 64 + ".pdf"]
    assert dict(report.states) == {"parsed": 1}

    async with session_factory() as s:
        doc = (
            await s.execute(select(Document).where(Document.source_url.like("%/a.pdf")))
        ).scalar_one()
        assert (
            doc.title == "Personal Data Protection Act 2012" and doc.instrument_family == "sg-pdpa"
        )
        v = (
            await s.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc.id))
        ).scalar_one()
        assert v.official_number == "Act 1 of 2012" and v.version_label == "Revised 2020"
        assert v.published_date == date(2012, 10, 15) and v.effective_date == date(2014, 7, 2)
        assert v.legal_status == "unknown"  # 一期"带官方日期"不等于现行有效
        assert v.metadata_source == "phase1_import" and v.state == "parsed"
        # 一期时间按 UTC+8 解释并转换为 UTC
        assert v.fetched_at == datetime(2026, 10, 19, 2, 45, 52, tzinfo=UTC)
        clauses = (await s.execute(select(Clause).where(Clause.version_id == v.id))).scalars().all()
        assert len(clauses) == 2
        assert archive.read(v.archive_path) == good
        # 迁移来的站点一律停用、条款待确认,且不会被排期
        sites = (await s.execute(select(SourceSite))).scalars().all()
        assert len(sites) == 3 and all(not x.enabled and x.terms_status == "pending" for x in sites)
        assert all(x.next_run_at is None for x in sites)


async def test_import_is_idempotent_and_country_scoped(session_factory, tmp_path):
    data, _, _ = build_phase1_dir(tmp_path)
    archive = ArchiveStore(tmp_path / "archive")
    await import_phase1(data, "SG", session_factory, archive)
    again = await import_phase1(data, "SG", session_factory, archive)
    assert again.versions_new == 0 and again.documents_new == 0 and again.already_present == 1
    async with session_factory() as s:
        assert {d.country_code for d in (await s.execute(select(Document))).scalars()} == {"SG"}


async def test_import_rejects_tampered_file_without_touching_archive(session_factory, tmp_path):
    data, _, _ = build_phase1_dir(tmp_path)
    archive = ArchiveStore(tmp_path / "archive")
    await import_phase1(data, "SG", session_factory, archive)
    async with session_factory() as s:
        versions = (await s.execute(select(DocumentVersion))).scalars().all()
        assert len(versions) == 1  # 哈希不一致的那份没有进库
    stored = [p for p in (tmp_path / "archive").rglob("*") if p.is_file()]
    assert len(stored) == 1
