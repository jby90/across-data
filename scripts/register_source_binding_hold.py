"""Record a source binding anomaly in the isolated copy; do not change source or approvals."""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select

from app import models_registry  # noqa: F401
from app.analysis.models import ReportVersion
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Document, DocumentVersion
from app.ingestion.parsing import parse_document
from app.ingestion.safety import report_held_citation_ids
from app.knowledge.history import propose, snapshot


async def register(document_id, json_path):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only an isolated remediation copy is allowed")
    async with get_sessionmaker()() as session:
        doc = await session.get(Document, document_id)
        version = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == document_id
        ).order_by(DocumentVersion.version_no.desc()).limit(1))
        if not doc or not version:
            raise ValueError("document/version missing")
        raw = ArchiveStore(settings.archive_dir).read(version.archive_path)
        parsed = parse_document(raw, version.mime_type)
        first_page = parsed.pages[0]
        if not ("Personal Data Protection Act" in doc.title
                and "Application for Trial of Autonomous Motor Vehicles" in first_page):
            raise ValueError("expected anomaly not confirmed; no hold created")
        payload = dict(safety_hold=True, reason="原文绑定异常：PDPA 条目实际绑定了 LTA 自动驾驶申请表；"
            "已保留原文及审批，待重新取得正确官方文件并人工核实。",
            document_id=str(doc.id), version_id=str(version.id), source_url=doc.source_url,
            content_hash=version.content_hash, actual_pages=len(parsed.pages),
            first_page_evidence=first_page[:500], requested_resolution="reacquire_and_review_new_revision")
        proposal = await propose(session, doc, payload)
        snapshot(session, doc, "source_binding_anomaly_registered")
        await session.commit()
        report_impacts = []
        for report in (await session.scalars(select(ReportVersion))).all():
            held = await report_held_citation_ids(session, report)
            if held:
                report_impacts.append(dict(report_id=str(report.id), version_no=report.version_no,
                                           affected_clauses=[str(cid) for cid in held]))
        result = dict(proposal_id=str(proposal.id), source_and_approvals_unchanged=True,
            evidence=payload, report_impacts=report_impacts)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(dict(proposal_id=str(proposal.id), affected_reports=len(report_impacts),
                             source_and_approvals_unchanged=True), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", type=uuid.UUID, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(register(args.document_id, args.json))
