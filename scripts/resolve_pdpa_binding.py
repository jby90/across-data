"""Append a canonical PDPA archive association in the isolated remediation copy only."""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.ingestion.bindings import record_canonical_binding


async def resolve(output):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only isolated remediation databases are permitted")
    archive = ArchiveStore(settings.archive_dir)
    async with get_sessionmaker()() as session:
        record = await record_canonical_binding(session, archive,
            uuid.UUID("24d1b591-0369-41a4-b47f-3c9ca870b153"),
            uuid.UUID("26aee7c0-25af-4887-90f5-4d82b9de0fc0"),
            expected_identity="PERSONAL DATA PROTECTION ACT 2012",
            expected_edition="2020 REVISED EDITION",
            acquisition_note="Existing independent official-URL archive; cover and SHA verified. "
                "Observed official HTTP 200 matches archive. Initial refetch exposed a legacy "
                "robots-403 permission bug; fixed policy blocks further automated downloads. "
                "This is not a compliant-crawl or current-law approval; human review pending.")
        await session.commit()
        result = dict(audit_id=str(record.id), **record.payload)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: result[key] for key in (
            "audit_id", "canonical_document_id", "pages", "review_status", "old_binding_remains_held"
        )}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", required=True, type=Path)
    asyncio.run(resolve(parser.parse_args().json))
