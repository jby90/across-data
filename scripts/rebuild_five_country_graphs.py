"""Full five-country rebuild on an isolated copy; atomic country replacement, bounded retries.

Successful clause evidence is cached append-only. No sampling, source rewrite or approval.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select

from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.knowledge.extractor import extract_knowledge_for_country
from app.knowledge.models import KgEdge, KgNode


async def run(output, concurrency, attempts, countries):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("only isolated remediation copies are permitted")
    if output.exists() or output.with_suffix(".jsonl").exists():
        raise ValueError("refusing to overwrite prior execution evidence")
    if not settings.llm_api_key:
        raise ValueError("an environment-only model key is required")
    started, clock = datetime.now(UTC), time.perf_counter()
    async with get_sessionmaker()() as session:
        old_edges = set((await session.scalars(select(KgEdge.id))).all())
        old_nodes = set((await session.scalars(select(KgNode.id))).all())
    results = []
    for cc in countries:
        for attempt in range(1, attempts + 1):
            print(json.dumps(dict(country=cc, attempt=attempt, event="full_rebuild_started")), flush=True)
            async with get_sessionmaker()() as session:
                result = await extract_knowledge_for_country(session, cc,
                    translate_sample_per_doc=0, max_clauses_per_doc=None, concurrency=concurrency)
            record = dict(country=cc, attempt=attempt, recorded_at=datetime.now(UTC).isoformat(), **result)
            results.append(record)
            with output.with_suffix(".jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps({k:record[k] for k in ("country", "attempt", "status", "reason",
                "attempted_clauses", "failed_clauses", "cache_hits", "semantic_edges_added",
                "model_usage", "duration_ms") if k in record}, ensure_ascii=False), flush=True)
            if result.get("status") == "completed":
                break
    async with get_sessionmaker()() as session:
        edges = list((await session.scalars(select(KgEdge))).all())
        nodes = list((await session.scalars(select(KgNode))).all())
        new_edges = [row for row in edges if row.id not in old_edges]
        new_nodes = [row for row in nodes if row.id not in old_nodes]
        latest = {cc: next(row for row in reversed(results) if row["country"] == cc)
                  for cc in countries}
        summary = dict(started_at=started.isoformat(), completed_at=datetime.now(UTC).isoformat(),
            duration_seconds=round(time.perf_counter()-clock, 3),
            requested_countries=list(countries),
            all_countries_completed=all(row.get("status") == "completed" for row in latest.values()),
            no_sampling=all(row.get("sampled") is False for row in latest.values()
                            if row.get("status") == "completed"),
            new_edges=len(new_edges), new_nodes=len(new_nodes),
            all_new_edges_pending=all(row.review_status == "pending_review" for row in new_edges),
            all_new_nodes_pending=all(row.review_status == "pending_review" for row in new_nodes),
            human_approved=0, latest_by_country=latest,
            tokens_in=sum(row.get("model_usage", {}).get("tokens_in", 0) for row in results),
            tokens_out=sum(row.get("model_usage", {}).get("tokens_out", 0) for row in results))
    output.write_text(json.dumps(dict(summary=summary, attempts=results), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=16)
    parser.add_argument("--attempts", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--countries", nargs="+", choices=("EG", "TN", "MA", "LB", "SG"),
                        default=["EG", "TN", "MA", "LB", "SG"])
    args = parser.parse_args()
    asyncio.run(run(args.json, args.concurrency, args.attempts, tuple(dict.fromkeys(args.countries))))
