"""Repair two frozen PDF layouts in the isolated copy; retain all historical rows."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.archive import ArchiveStore
from app.knowledge.source_repairs import append_page_repair

TARGETS = [
    (
        "f0fd255b-0f1f-4ce5-80bc-da26ae8f56cd",
        16,
        "ac1b9cd1914d7c00df391323f3d0952e0381974dab951e7782b4f4b895b8f75f",
    ),
    (
        "41ed474f-6426-49ea-9f61-d9d614bf74e5",
        40,
        "e88076a37faf78473dcef5f67cf7ee7d34bd9960c38dcaf8d3a92e2f80785c06",
    ),
]


async def run(output):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("isolated remediation database required")
    if output.exists():
        raise ValueError("existing run evidence must not be overwritten")
    if not settings.llm_api_key:
        raise ValueError("real LLM key required in environment")
    started, rows = time.perf_counter(), []
    for clause_id, page_number, digest in TARGETS:
        async with get_sessionmaker()() as session:
            revision, proposal, bilingual, reused = await append_page_repair(
                session,
                ArchiveStore(settings.archive_dir),
                clause_id,
                page_number,
                digest,
            )
            await session.commit()
            payload = bilingual.payload
            row = {
                "clause_id": clause_id,
                "source_revision_id": str(revision.id),
                "proposal_id": str(proposal.id),
                "bilingual_revision_id": str(bilingual.id),
                "cached": reused,
                "prepared": revision.payload["prepared"],
                "translation": payload,
            }
            rows.append(row)
            print(
                json.dumps(
                    {
                        "clause_id": clause_id,
                        "units": len(payload["units"]),
                        "failed": sum(
                            not u["quality_check_passed"] for u in payload["units"]
                        ),
                        "pending_review": True,
                        "cached": reused,
                    }
                ),
                flush=True,
            )
    summary = {
        "targets": len(rows),
        "source_overwrites": 0,
        "human_approved": 0,
        "linguistic_units": sum(len(r["translation"]["units"]) for r in rows),
        "failed_units": sum(
            not u["quality_check_passed"]
            for r in rows
            for u in r["translation"]["units"]
        ),
        "tokens_in": sum(
            r["translation"]["tokens_in"] for r in rows if not r["cached"]
        ),
        "tokens_out": sum(
            r["translation"]["tokens_out"] for r in rows if not r["cached"]
        ),
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"summary": summary, "items": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary), flush=True)
    if summary["failed_units"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    asyncio.run(run(parser.parse_args().json))
