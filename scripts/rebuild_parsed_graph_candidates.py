"""Process complete repaired pages into pending candidates, without activating legal evidence."""

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
from app.knowledge.kg_run_state import begin_usage, end_usage
from app.knowledge.parsed_graph import pending_parsed_sources, propose_parsed_graph


async def run(output):
    settings = get_settings()
    if (
        not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_")
        or not settings.llm_api_key
    ):
        raise ValueError("isolated remediation DB and environment-only model key required")
    if output.exists():
        raise ValueError("prior evidence must not be overwritten")
    token, usage = begin_usage()
    started, items = time.perf_counter(), []
    try:
        async with get_sessionmaker()() as session:
            for source in await pending_parsed_sources(session):
                record, reused = await propose_parsed_graph(
                    session, source, ArchiveStore(settings.archive_dir)
                )
                await session.commit()
                items.append(dict(id=str(record.id), cached=reused, payload=record.payload))
    finally:
        end_usage(token)
    result = dict(
        items=items,
        usage=usage,
        duration_seconds=round(time.perf_counter() - started, 3),
        human_approvals=0,
        active_edges_added=0,
        all_completed=all(r["payload"]["status"] == "completed" for r in items),
    )
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    if not result["all_completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    asyncio.run(run(parser.parse_args().json))
