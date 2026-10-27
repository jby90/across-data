"""Register already archived, robots-permitted sources in an isolated copy only.

No model calls, automatic approvals, source overwrites or scheduled crawling.
Identity keys are supplied in a source manifest, never inferred from evaluation IDs.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import models_registry  # noqa: F401
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Document, DocumentVersion
from app.ingestion.pipeline import parse_version
from app.ingestion.source_observations import observation_text, validate_observation
from app.knowledge.history import payload_hash
from app.knowledge.models import KnowledgeAuditRecord
from app.sources.models import SourceSite
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def run(args):
    if not args.database.startswith("remediation_") or args.output.exists():
        raise ValueError("new isolated output required")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    archive = ArchiveStore(args.archive_root)
    engine = create_async_engine(
        "postgresql+asyncpg://postgres@127.0.0.1:3739/" + args.database
    )
    results = []
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:  # noqa: SIM117
            async with session.begin():
                for source in manifest["new_documents"]:
                    if not source["acquisition"]["robots_permitted"]:
                        raise ValueError("robots permission unconfirmed")
                    blob = archive.read(source["archive_path"])
                    if archive.sha256(blob) != source["content_hash"]:
                        raise ValueError("archive hash differs")
                    doc = await session.scalar(
                        select(Document).where(
                            Document.country_code == source["country_code"],
                            Document.source_url == source["source_url"],
                        )
                    )
                    if doc:
                        raise ValueError(
                            "existing document must use a separate reviewed revision"
                        )
                    site = SourceSite(
                        country_code=source["country_code"],
                        publisher=source["publisher"],
                        language=source["language"],
                        document_type="LAW",
                        entry_url=source["source_url"],
                        strategy="DIRECT",
                        enabled=False,
                        terms_status="pending",
                        terms_note="一次性官方渠道研究归档；计划采集仍待负责人确认。",
                    )
                    session.add(site)
                    await session.flush()
                    doc = Document(
                        site_id=site.id,
                        country_code=site.country_code,
                        source_url=site.entry_url,
                        title=source["title"],
                        language=site.language,
                        document_type="LAW",
                        instrument_kind="LAW",
                        relevance="relevant",
                    )
                    session.add(doc)
                    await session.flush()
                    version = DocumentVersion(
                        document_id=doc.id,
                        version_no=1,
                        content_hash=source["content_hash"],
                        archive_path=source["archive_path"],
                        size_bytes=len(blob),
                        mime_type=source["mime"],
                        final_url=source["source_url"],
                        state="downloaded",
                        review_status="pending_review",
                        legal_status="unknown",
                        version_label="Statutory body republication — pending verification",
                    )
                    session.add(version)
                    await session.flush()
                    await parse_version(session, archive, version, source["language"])
                    if version.state != "parsed":
                        raise ValueError(
                            "new source parse failed: " + str(version.error)
                        )
                    acquisition = dict(
                        **source["acquisition"],
                        source_url=doc.source_url,
                        content_hash=version.content_hash,
                        human_review=None,
                        model_calls=0,
                    )
                    session.add(
                        KnowledgeAuditRecord(
                            target_type="documents",
                            target_id=doc.id,
                            action="official_source_acquired",
                            payload=acquisition,
                            content_hash=payload_hash(acquisition),
                        )
                    )
                    source["observation"].update(version_id=str(version.id))
                    manifest["observations"].append(
                        {"document_id": str(doc.id), "payload": source["observation"]}
                    )
                    results.append(
                        {
                            "document_id": str(doc.id),
                            "version_id": str(version.id),
                            "review_status": "pending_review",
                            "legal_status": "unknown",
                            "source_url": doc.source_url,
                        }
                    )
                for item in manifest["observations"]:
                    doc = await session.get(Document, uuid.UUID(item["document_id"]))
                    payload = item["payload"]
                    version = await session.get(
                        DocumentVersion, uuid.UUID(payload["version_id"])
                    )
                    if version.document_id != doc.id:
                        raise ValueError("observation attached to another document")
                    blob = archive.read(version.archive_path)
                    if archive.sha256(blob) != version.content_hash:
                        raise ValueError("version archive mismatch")
                    validate_observation(
                        payload, version, doc, observation_text(blob, version.mime_type)
                    )
                    digest = payload_hash(payload)
                    prior = await session.scalar(
                        select(KnowledgeAuditRecord).where(
                            KnowledgeAuditRecord.target_id == doc.id,
                            KnowledgeAuditRecord.action == "source_identity_observed",
                            KnowledgeAuditRecord.content_hash == digest,
                        )
                    )
                    if prior is None:
                        session.add(
                            KnowledgeAuditRecord(
                                target_type="documents",
                                target_id=doc.id,
                                action="source_identity_observed",
                                payload=payload,
                                content_hash=digest,
                            )
                        )
    finally:
        await engine.dispose()
    result = {
        "documents": results,
        "identity_observations": len(manifest["observations"]),
        "source_overwrites": 0,
        "new_human_approvals": 0,
        "model_calls": 0,
        "scheduled_crawlers_enabled": 0,
    }
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database", required=True)
    p.add_argument("--archive-root", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
