"""Retry the frozen 108-clause gap list, retaining every previous attempt and source row."""

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select

from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.models import Clause, DocumentVersion
from app.ingestion.safety import document_is_held
from app.knowledge.models import ClauseTranslation
from app.knowledge.translation_context import is_numeric_fragment, translate_fragment_context
from app.knowledge.translator import set_translation_concurrency, translate_clause


async def run(input_path, output_path, concurrency):
    if not get_settings().database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only isolated remediation copies are permitted")
    targets = json.loads(input_path.read_text(encoding="utf-8"))["invalid_current"]
    started = datetime.now(UTC)
    clock = time.perf_counter()
    sem = asyncio.Semaphore(concurrency)
    set_translation_concurrency(concurrency)
    results = []

    async def worker(item):
        async with sem:
            async with get_sessionmaker()() as session:
                try:
                    clause = await session.get(Clause, uuid.UUID(item["clause_id"]))
                    if not clause or clause.original_text != item["original"]:
                        raise ValueError("frozen source differs; refused")
                    version = await session.get(DocumentVersion, clause.version_id)
                    if await document_is_held(session, version.document_id):
                        raise ValueError("source held; refused")
                    if is_numeric_fragment(clause.original_text) or item.get("context_revision"):
                        record = await translate_fragment_context(session, clause,
                            density_context=bool(item.get("context_revision")))
                        payload = record.payload
                        new_call = record.created_at >= started
                        result = dict(clause_id=str(clause.id), path="context_revision",
                            record_id=str(record.id), passed=payload["quality_check_passed"],
                            review_status=payload["review_status"], issues=payload["quality_issues"],
                            tokens_in=payload["tokens_in"] if new_call else 0,
                            tokens_out=payload["tokens_out"] if new_call else 0)
                    else:
                        old_ids = set((await session.scalars(select(ClauseTranslation.id).where(
                            ClauseTranslation.clause_id == clause.id))).all())
                        tr = await translate_clause(session, clause, item["country_code"])
                        new_call = tr.id not in old_ids
                        result = dict(clause_id=str(clause.id), path="standalone_translation",
                            record_id=str(tr.id), passed=tr.quality_check_passed,
                            review_status=tr.review_status, issues=tr.quality_issues,
                            tokens_in=tr.tokens_in if new_call else 0,
                            tokens_out=tr.tokens_out if new_call else 0)
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    result = dict(clause_id=item["clause_id"], path="error", passed=False,
                                  error_type=type(exc).__name__, tokens_in=0, tokens_out=0)
                results.append(result)
                if len(results) % 10 == 0 or len(results) == len(targets):
                    print(json.dumps(dict(completed=len(results), total=len(targets),
                        passed=sum(row["passed"] for row in results)), ensure_ascii=False), flush=True)

    await asyncio.gather(*(worker(item) for item in targets))
    summary = dict(targets=len(targets), standalone_passed=sum(row["passed"] and row["path"] ==
        "standalone_translation" for row in results), context_passed=sum(row["passed"] and row["path"] ==
        "context_revision" for row in results), failed=sum(not row["passed"] for row in results),
        tokens_in=sum(row["tokens_in"] for row in results), tokens_out=sum(row["tokens_out"] for row in results),
        duration_seconds=round(time.perf_counter()-clock, 3), human_approved=0,
        started_at=started.isoformat(), completed_at=datetime.now(UTC).isoformat())
    output_path.write_text(json.dumps(dict(summary=summary, results=results),
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4, choices=range(1, 17))
    args = parser.parse_args()
    asyncio.run(run(args.input, args.json, args.concurrency))
